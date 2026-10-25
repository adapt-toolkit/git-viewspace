import contextlib
import errno
import io
import json
import os
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from git_viewspace.cli import _parser as cli_parser
from git_viewspace.cli import run as cli_run
from git_viewspace import service
from git_viewspace.daemon import Daemon
from git_viewspace.errors import GitViewspaceError, UsageError
from git_viewspace.git import Git
from git_viewspace.host import Mount, process_start_time_ticks
from git_viewspace.init import InitRequest, Initializer
from git_viewspace.protocol import (
    call,
    decode_message,
    receive_message,
    request,
    send_message,
    validate_request,
)
from git_viewspace.root import RootContext
from git_viewspace.schemas import RUNTIME_FORMAT, SCHEMA_VERSION, validate_runtime
from git_viewspace.storage import atomic_json
from git_viewspace.service import start_background, status, stop
from git_viewspace.schemas import SYNC_FORMAT, sync_identifier, sync_manifest_digest, validate_sync
from git_viewspace.sync import OverlaySync, comparable_report, validate_report
from git_viewspace.viewfs import ViewFS
from git_viewspace.views import ReadonlyMounts


class _FakeProcess:
    def __init__(self):
        self.pid = os.getpid()
        self.alive = True
        self.returncode = None

    def poll(self):
        return None if self.alive else self.returncode

    def wait(self, timeout=None):
        self.alive = False
        self.returncode = 0
        return 0

    def terminate(self):
        self.alive = False
        self.returncode = -15


class _MemoryStream:
    def __init__(self, incoming=b""):
        self.incoming = bytearray(incoming)
        self.sent = bytearray()

    def settimeout(self, timeout):
        self.timeout = timeout

    def recv(self, size):
        if not self.incoming:
            return b""
        result = bytes(self.incoming[:size])
        del self.incoming[:size]
        return result

    def sendall(self, payload):
        self.sent.extend(payload)


class _FakeClientSocket(_MemoryStream):
    def __init__(self, connect_failures):
        super().__init__(b'{"ok":true}\n')
        self.connect_failures = connect_failures
        self.connect_calls = 0
        self.timeout = None
        self.closed = False

    def settimeout(self, timeout):
        self.timeout = timeout

    def connect(self, _path):
        self.connect_calls += 1
        if self.connect_calls <= self.connect_failures:
            raise BlockingIOError(errno.EAGAIN, os.strerror(errno.EAGAIN))

    def close(self):
        self.closed = True


class _FakeServer:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class _FakeViewFS:
    timeout = 0.25

    def __init__(self, context):
        self.context = context
        self.running = False
        self.generation = 0
        self.process = _FakeProcess()
        self.source = None
        self.attempt_token = None
        self.cleaned_sources = []

    def start(self):
        self.generation += 1
        self.running = True
        self.process = _FakeProcess()
        self.attempt_token = f"attempt-{self.generation}"
        self.source = f"GitViewspaceFS:{self.context.root_uuid}:{self.attempt_token}"
        return self.runtime()

    def stop(self, *, force=True):
        self.running = False
        self.process.alive = False

    def healthy(self, observed=None):
        del observed
        return self.running

    def runtime(self):
        return {
            "attempt_token": self.attempt_token,
            "source": self.source,
            "supervisor_pid": os.getpid(),
            "supervisor_start_time_ticks": process_start_time_ticks(),
        }

    def cleanup_mount(self, source, *, force=True):
        self.cleaned_sources.append(source)
        self.running = False

    def remove_diagnostics(self):
        pass


class _FakeMounts:
    def __init__(self, context):
        self.context = context
        self.mounted = set()
        self.probed = []

    def probe(self, view):
        self.probed.append(view.alias)

    def observe(self, view, source, observed=None):
        del source, observed
        return object() if view.name in self.mounted else None

    def start(self, view, source):
        del source
        already = view.name in self.mounted
        self.mounted.add(view.name)
        return already

    def stop(self, view, source, *, force=False):
        del source, force
        already = view.name not in self.mounted
        self.mounted.discard(view.name)
        return already

    def cleanup_all(self, views, source, *, force):
        del views, source, force
        self.mounted.clear()


class _FakeGit:
    def __init__(self, commits, branches=None):
        self.commits = commits
        self.branches = branches or {"main": commits.get("HEAD")}

    def resolve_commit(self, repository, commitish):
        del repository
        try:
            return self.commits[commitish]
        except KeyError as error:
            raise GitViewspaceError("COMMIT_NOT_FOUND", f"cannot resolve commit: {commitish}") from error

    def resolve_local_branch(self, repository, branch):
        del repository
        try:
            return self.branches[branch]
        except KeyError as error:
            raise GitViewspaceError("BRANCH_NOT_FOUND", f"local branch does not exist: {branch}") from error

    def resolve_direct_local_branch(self, repository, branch):
        return self.resolve_local_branch(repository, branch)

    def create_local_branch(self, repository, branch, parent_branch):
        commit = self.resolve_local_branch(repository, parent_branch)
        if branch in self.branches:
            raise GitViewspaceError("BRANCH_EXISTS", f"local branch already exists: {branch}")
        self.branches[branch] = commit
        return commit

    def checkout_position(self, repository):
        del repository
        return self.commits["HEAD"], "main"

    def checkout_status(self, repository):
        del repository
        return b""

    def validate_redirect_sources(self, repository, base_commit, redirects):
        del repository, base_commit, redirects

    def prepare_sync_checkout(self, repository, branch, base_commit):
        del repository, branch, base_commit
        return "b" * 40


class _FakeSync:
    def __init__(self, report=None):
        self.report = report or {
            "mode": "inspect",
            "workload_uid": os.getuid(),
            "workload_gid": os.getgid(),
            "upper_fingerprint": "a" * 64,
            "git_pathspecs": ["created.txt", "deleted.txt"],
            "counts": {
                "directories": 1,
                "regulars": 1,
                "symlinks": 0,
                "whiteouts": 1,
                "opaque": 0,
                "origin": 0,
                "impure": 0,
            },
            "redirects": [],
            "whiteouts": ["deleted.txt"],
            "opaque": [],
            "cleanup_pending": False,
        }
        self.calls = []

    def inspect(self, upper):
        self.calls.append(("inspect", Path(upper)))
        return dict(self.report)

    def replay(self, upper, checkout):
        self.calls.append(("replay", Path(upper), Path(checkout)))
        return {**self.report, "mode": "direct"}


class Stage2Test(unittest.TestCase):
    def setUp(self):
        # Harness-owned workspaces add path components before this directory; keep socket paths
        # short without moving test artifacts outside the selected scratch root.
        self.temporary = tempfile.TemporaryDirectory(prefix="v2-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.home = self.base / "home"
        self.home.mkdir()
        self.environment = mock.patch.dict(
            os.environ,
            {
                "HOME": str(self.home),
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_TERMINAL_PROMPT": "0",
            },
            clear=False,
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.root = self.base / "root"
        Initializer().initialize(
            InitRequest(
                root=self.root,
                git_name="Managed User",
                git_email="managed@example.invalid",
                source_mode="empty",
            )
        )

    def context(self):
        return RootContext.load(self.root)

    @staticmethod
    def _commit_file(repository, branch, path, contents, message):
        subprocess.run(
            ("git", "-C", str(repository), "checkout", "--force", branch),
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        (repository / path).write_text(contents, encoding="utf-8")
        subprocess.run(("git", "-C", str(repository), "add", "--", path), check=True)
        subprocess.run(
            (
                "git",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "commit.gpgSign=false",
                "-c",
                "user.name=Managed User",
                "-c",
                "user.email=managed@example.invalid",
                "-C",
                str(repository),
                "commit",
                "--no-gpg-sign",
                "--no-verify",
                "-m",
                message,
            ),
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return (
            subprocess.run(
                ("git", "-C", str(repository), "rev-parse", "HEAD"),
                check=True,
                stdout=subprocess.PIPE,
            )
            .stdout.decode("ascii")
            .strip()
        )

    @staticmethod
    def mounted(source):
        return Mount(Path("/unused"), "/", "fuse", source, frozenset({"ro"}), frozenset({"ro"}))

    @staticmethod
    def guarded_requests():
        return {
            "view-init-overlay": {"view": "docs", "branch": "main"},
            "view-start": {"view": "docs"},
            "view-stop": {"view": "docs"},
            "view-sync": {"view": "docs"},
            "view-stage": {"view": "docs"},
            "view-commit": {"view": "docs", "message": "message"},
            "view-advance": {"view": "docs", "message": "message"},
            "view-status": {"view": "docs"},
            "view-switch": {"view": "docs", "to": "HEAD", "next": False},
            "view-clean": {"view": "docs"},
            "view-deinit-overlay": {"view": "docs"},
            "view-destroy": {"view": "docs", "discard": False},
        }

    def test_protocol_round_trip_and_strict_request(self):
        self.assertEqual(request("status"), {"protocol": 4, "command": "status"})
        encoded = _MemoryStream()
        send_message(encoded, request("status"))
        decoded = _MemoryStream(bytes(encoded.sent))
        self.assertEqual(receive_message(decoded, "request"), request("status"))
        self.assertEqual(validate_request(request("stop"))["command"], "stop")
        branch_merge = request(
            "branch-merge",
            attempt_branch="runner-docs",
            parent_branch="main",
            ensure_attempt_tip="1" * 40,
            message="Merge runner-docs",
            checked_out_destination="refuse",
        )
        self.assertEqual(validate_request(branch_merge), branch_merge)
        self.assertEqual(
            validate_request(request("view-init-readonly", view="docs", at="HEAD"))["view"],
            "docs",
        )
        self.assertEqual(
            validate_request(request("view-init-overlay", view="docs", branch="main"))["branch"],
            "main",
        )
        self.assertEqual(
            validate_request(request("view-sync", view="docs"))["command"],
            "view-sync",
        )
        self.assertEqual(
            validate_request(request("view-stage", view="docs"))["command"],
            "view-stage",
        )
        self.assertEqual(
            validate_request(request("view-commit", view="docs", message="message"))["message"],
            "message",
        )
        self.assertEqual(
            validate_request(
                request(
                    "view-init-and-start",
                    view="docs",
                    kind="overlay",
                    from_branch="main",
                    new_branch="runner-docs",
                )
            )["kind"],
            "overlay",
        )
        self.assertEqual(
            validate_request(request("view-advance", view="docs", message="message"))["command"],
            "view-advance",
        )
        self.assertTrue(
            validate_request(request("view-switch", view="docs", to=None, next=True))["next"]
        )
        destroy = request(
            "view-destroy",
            view="docs",
            discard=False,
            ensure_view_id="1" * 32,
        )
        self.assertEqual(validate_request(destroy)["ensure_view_id"], "1" * 32)
        self.assertNotIn(
            "ensure_view_id",
            validate_request(request("view-destroy", view="docs", discard=False)),
        )
        for invalid_view_id in ("1" * 31, "A" * 32, 1, None):
            with self.assertRaises(GitViewspaceError) as caught:
                validate_request(
                    request(
                        "view-destroy",
                        view="docs",
                        discard=False,
                        ensure_view_id=invalid_view_id,
                    )
                )
            self.assertEqual(caught.exception.code, "INVALID_REQUEST")
        with self.assertRaises(GitViewspaceError):
            validate_request(request("view-switch", view="docs", to="HEAD", next=True))
        self.assertIsNone(validate_request(request("view-status", view=None))["view"])
        with self.assertRaises(GitViewspaceError) as caught:
            validate_request({"protocol": 3, "command": "status"})
        self.assertEqual(caught.exception.code, "INVALID_REQUEST")
        for invalid_policy in ("detach-if-clean", "unknown", "", None, True):
            with self.subTest(invalid_policy=invalid_policy):
                invalid = {**branch_merge, "checked_out_destination": invalid_policy}
                with self.assertRaises(GitViewspaceError) as caught:
                    validate_request(invalid)
                self.assertEqual(caught.exception.code, "INVALID_REQUEST")

    def test_protocol_retries_transient_socket_admission_before_dispatch(self):
        client = _FakeClientSocket(connect_failures=1)
        with (
            mock.patch("git_viewspace.protocol.socket.socket", return_value=client),
            mock.patch("git_viewspace.protocol.time.sleep") as sleep,
        ):
            response = call(self.root / "run/control.sock", "status", timeout=30.0)
        self.assertTrue(response["ok"])
        self.assertEqual(client.connect_calls, 2)
        sleep.assert_called_once_with(5.0)
        self.assertTrue(client.sent)
        self.assertTrue(client.closed)

    def test_protocol_bounds_persistent_socket_admission_failure(self):
        client = _FakeClientSocket(connect_failures=100)
        with (
            mock.patch("git_viewspace.protocol.socket.socket", return_value=client),
            mock.patch("git_viewspace.protocol.time.sleep") as sleep,
            self.assertRaises(GitViewspaceError) as caught,
        ):
            call(self.root / "run/control.sock", "status", timeout=60.0)
        self.assertEqual(caught.exception.code, "DAEMON_BUSY")
        self.assertIn("Resource temporarily unavailable", caught.exception.detail)
        self.assertEqual(client.connect_calls, 7)
        self.assertEqual(sleep.call_count, 6)
        self.assertFalse(client.sent)
        self.assertTrue(client.closed)

    def test_daemon_control_socket_admits_twenty_queued_clients(self):
        context = self.context()
        daemon = Daemon(context, viewfs_factory=_FakeViewFS)
        clients = []
        try:
            daemon._bind_socket()
            for _index in range(20):
                client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                client.settimeout(0.1)
                client.connect(str(context.socket_path))
                clients.append(client)
        finally:
            for client in clients:
                client.close()
            if daemon.server is not None:
                daemon.server.close()
            try:
                context.socket_path.unlink()
            except FileNotFoundError:
                pass
        self.assertEqual(len(clients), 20)

    def test_branch_pin_protocol(self):
        context, daemon = self._started_real_git_view_daemon()
        commit = context.repository_facts.head
        branch = "viewspace/publications/runner/000001"
        wire_request = {
            "protocol": 4,
            "command": "branch-pin",
            "branch": branch,
            "commit": commit,
        }
        self.assertEqual(request("branch-pin", branch=branch, commit=commit), wire_request)
        self.assertEqual(validate_request(wire_request), wire_request)
        for invalid in (
            {**wire_request, "unexpected": True},
            {key: value for key, value in wire_request.items() if key != "branch"},
            {**wire_request, "branch": True},
            {**wire_request, "commit": None},
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(GitViewspaceError) as caught:
                    validate_request(invalid)
                self.assertEqual(caught.exception.code, "INVALID_REQUEST")

        views_before = tuple(sorted(daemon.store.views))
        view_paths_before = tuple(sorted(path.name for path in (self.root / "views").iterdir()))
        expected = {
            "ok": True,
            "command": "branch pin",
            "root": str(context.root),
            "root_uuid": context.root_uuid,
            "branch": branch,
            "commit": commit,
            "created": True,
        }
        created = self._daemon_request(
            daemon,
            "branch-pin",
            branch=branch,
            commit=commit,
        )
        self.assertEqual(created, expected)
        self.assertEqual(daemon.git.resolve_direct_local_branch(context.repository, branch), commit)

        repeated = self._daemon_request(
            daemon,
            "branch-pin",
            branch=branch,
            commit=commit,
        )
        self.assertEqual(repeated, {**expected, "created": False})

        projected = {**expected, "created": False}
        with (
            mock.patch("git_viewspace.service.OnlineRoot.load", return_value=context),
            mock.patch("git_viewspace.service.call", return_value=projected) as socket_call,
        ):
            self.assertEqual(service.branch_pin(self.root, branch, commit), projected)
        socket_call.assert_called_once_with(
            context.socket_path,
            "branch-pin",
            branch=branch,
            commit=commit,
        )
        self.assertEqual(tuple(sorted(daemon.store.views)), views_before)
        self.assertEqual(
            tuple(sorted(path.name for path in (self.root / "views").iterdir())),
            view_paths_before,
        )

    def test_branch_pin_guards_and_cli(self):
        context, daemon = self._started_real_git_view_daemon()
        repository = context.repository
        original_commit = context.repository_facts.head
        next_commit = self._commit_file(
            repository,
            "main",
            "next.txt",
            "next\n",
            "Next",
        )
        views_before = tuple(sorted(daemon.store.views))
        view_paths_before = tuple(sorted(path.name for path in (self.root / "views").iterdir()))

        diverged_branch = "viewspace/publications/runner/000002"
        first = self._daemon_request(
            daemon,
            "branch-pin",
            branch=diverged_branch,
            commit=original_commit,
        )
        self.assertTrue(first["created"])
        diverged = self._daemon_request(
            daemon,
            "branch-pin",
            branch=diverged_branch,
            commit=next_commit,
        )
        self.assertFalse(diverged["ok"])
        self.assertEqual(diverged["command"], "branch-pin")
        self.assertEqual(diverged["error"]["code"], "BRANCH_DIVERGED")
        self.assertEqual(
            daemon.git.resolve_direct_local_branch(repository, diverged_branch),
            original_commit,
        )

        symbolic_branch = "viewspace/publications/runner/000003"
        subprocess.run(
            (
                "git",
                "-C",
                str(repository),
                "symbolic-ref",
                f"refs/heads/{symbolic_branch}",
                "refs/heads/main",
            ),
            check=True,
        )
        symbolic = self._daemon_request(
            daemon,
            "branch-pin",
            branch=symbolic_branch,
            commit=next_commit,
        )
        self.assertFalse(symbolic["ok"])
        self.assertEqual(symbolic["error"]["code"], "SYMBOLIC_BRANCH_UNSUPPORTED")

        subprocess.run(
            (
                "git",
                "-c",
                "user.name=Managed User",
                "-c",
                "user.email=managed@example.invalid",
                "-C",
                str(repository),
                "tag",
                "-a",
                "pin-annotated",
                "-m",
                "Annotated pin target",
                next_commit,
            ),
            check=True,
        )
        annotated_tag = (
            subprocess.run(
                ("git", "-C", str(repository), "rev-parse", "refs/tags/pin-annotated"),
                check=True,
                stdout=subprocess.PIPE,
            )
            .stdout.decode("ascii")
            .strip()
        )
        uppercase_commit = next_commit.upper()
        if uppercase_commit == next_commit:
            uppercase_commit = "A" + next_commit[1:]
        rejected_branch = "viewspace/publications/runner/rejected"
        for label, commit, expected_code in (
            ("malformed", "not-a-commit", "INVALID_COMMIT"),
            ("missing", "f" * 40, "COMMIT_NOT_FOUND"),
            ("symbolic-name", "HEAD", "INVALID_COMMIT"),
            ("abbreviated", next_commit[:12], "INVALID_COMMIT"),
            ("uppercase", uppercase_commit, "INVALID_COMMIT"),
            ("annotated-tag", annotated_tag, "INVALID_COMMIT"),
        ):
            with self.subTest(label=label):
                rejected = self._daemon_request(
                    daemon,
                    "branch-pin",
                    branch=rejected_branch,
                    commit=commit,
                )
                self.assertFalse(rejected["ok"])
                self.assertEqual(rejected["command"], "branch-pin")
                self.assertEqual(rejected["error"]["code"], expected_code)
        self.assertEqual(
            subprocess.run(
                (
                    "git",
                    "-C",
                    str(repository),
                    "show-ref",
                    "--verify",
                    "--quiet",
                    f"refs/heads/{rejected_branch}",
                ),
                check=False,
            ).returncode,
            1,
        )
        invalid_branch = self._daemon_request(
            daemon,
            "branch-pin",
            branch="invalid branch",
            commit=next_commit,
        )
        self.assertEqual(invalid_branch["error"]["code"], "INVALID_BRANCH")

        tree = (
            subprocess.run(
                ("git", "-C", str(repository), "rev-parse", f"{next_commit}^{{tree}}"),
                check=True,
                stdout=subprocess.PIPE,
            )
            .stdout.decode("ascii")
            .strip()
        )
        dangling_commit = (
            subprocess.run(
                (
                    "git",
                    "-c",
                    "user.name=Managed User",
                    "-c",
                    "user.email=managed@example.invalid",
                    "-C",
                    str(repository),
                    "commit-tree",
                    tree,
                    "-p",
                    next_commit,
                ),
                check=True,
                input=b"Dangling publication source\n",
                stdout=subprocess.PIPE,
            )
            .stdout.decode("ascii")
            .strip()
        )
        dangling_branch = "viewspace/publications/runner/000004"
        dangling = self._daemon_request(
            daemon,
            "branch-pin",
            branch=dangling_branch,
            commit=dangling_commit,
        )
        self.assertEqual(
            dangling,
            {
                "ok": True,
                "command": "branch pin",
                "root": str(context.root),
                "root_uuid": context.root_uuid,
                "branch": dangling_branch,
                "commit": dangling_commit,
                "created": True,
            },
        )

        concurrent_branch = "viewspace/publications/runner/000005"
        reference = f"refs/heads/{concurrent_branch}"
        racing_git = Git()
        original_repo = racing_git._repo
        winner = []

        def create_before_compare_and_update(repo, arguments, **keywords):
            if not winner and tuple(arguments[:3]) == (
                "update-ref",
                "--no-deref",
                reference,
            ):
                winner.append(Git().pin_local_branch(repo, concurrent_branch, next_commit))
            return original_repo(repo, arguments, **keywords)

        with mock.patch.object(
            racing_git,
            "_repo",
            side_effect=create_before_compare_and_update,
        ):
            loser = racing_git.pin_local_branch(repository, concurrent_branch, next_commit)
        self.assertEqual(
            winner,
            [{"branch": concurrent_branch, "commit": next_commit, "created": True}],
        )
        self.assertEqual(
            loser,
            {"branch": concurrent_branch, "commit": next_commit, "created": False},
        )

        cli_branch = "viewspace/publications/runner/000006"
        payload = {
            "ok": True,
            "command": "branch pin",
            "root": str(self.root),
            "root_uuid": context.root_uuid,
            "branch": cli_branch,
            "commit": next_commit,
            "created": True,
        }
        output = io.StringIO()
        with (
            mock.patch("git_viewspace.cli.branch_pin", return_value=payload) as operation,
            contextlib.redirect_stdout(output),
        ):
            result = cli_run(
                (
                    "--root",
                    str(self.root),
                    "--json",
                    "branch-pin",
                    "--branch",
                    cli_branch,
                    "--commit",
                    next_commit,
                )
            )
        self.assertEqual(result, 0)
        operation.assert_called_once_with(str(self.root), cli_branch, next_commit)
        self.assertEqual(json.loads(output.getvalue()), payload)

        failure_output = io.StringIO()
        with (
            mock.patch(
                "git_viewspace.cli.branch_pin",
                side_effect=GitViewspaceError(
                    "SYMBOLIC_BRANCH_UNSUPPORTED",
                    "local branch is symbolic rather than direct",
                ),
            ),
            contextlib.redirect_stdout(failure_output),
        ):
            failed = cli_run(
                (
                    "--root",
                    str(self.root),
                    "--json",
                    "branch-pin",
                    "--branch",
                    symbolic_branch,
                    "--commit",
                    next_commit,
                )
            )
        self.assertEqual(failed, 1)
        self.assertEqual(
            json.loads(failure_output.getvalue()),
            {
                "ok": False,
                "command": "branch-pin",
                "error": {
                    "code": "SYMBOLIC_BRANCH_UNSUPPORTED",
                    "message": "local branch is symbolic rather than direct",
                },
            },
        )
        command_token_branch_output = io.StringIO()
        with (
            mock.patch(
                "git_viewspace.cli.branch_pin",
                side_effect=GitViewspaceError("BRANCH_DIVERGED", "branch points elsewhere"),
            ),
            contextlib.redirect_stdout(command_token_branch_output),
        ):
            failed = cli_run(
                (
                    "--root",
                    str(self.root),
                    "--json",
                    "branch-pin",
                    "--branch",
                    "status",
                    "--commit",
                    next_commit,
                )
            )
        self.assertEqual(failed, 1)
        self.assertEqual(
            json.loads(command_token_branch_output.getvalue())["command"],
            "branch-pin",
        )
        self.assertEqual(tuple(sorted(daemon.store.views)), views_before)
        self.assertEqual(
            tuple(sorted(path.name for path in (self.root / "views").iterdir())),
            view_paths_before,
        )

    def test_protocol_accepts_optional_view_identity_only_for_existing_view_commands(self):
        expected_view_id = "1" * 32
        for command, arguments in self.guarded_requests().items():
            with self.subTest(command=command, mode="omitted"):
                self.assertNotIn(
                    "ensure_view_id",
                    validate_request(request(command, **arguments)),
                )
            with self.subTest(command=command, mode="guarded"):
                guarded = validate_request(
                    request(command, **arguments, ensure_view_id=expected_view_id)
                )
                self.assertEqual(guarded["ensure_view_id"], expected_view_id)

        for invalid_view_id in ("1" * 31, "1" * 33, "A" * 32, "z" * 32, 1, True, None, []):
            with self.subTest(invalid_view_id=invalid_view_id):
                with self.assertRaises(GitViewspaceError) as caught:
                    validate_request(
                        request(
                            "view-start",
                            view="docs",
                            ensure_view_id=invalid_view_id,
                        )
                    )
                self.assertEqual(caught.exception.code, "INVALID_REQUEST")

        excluded = (
            request("status", ensure_view_id=expected_view_id),
            request(
                "view-init-readonly",
                view="docs",
                at="HEAD",
                ensure_view_id=expected_view_id,
            ),
            request(
                "view-init-and-start",
                view="docs",
                kind="readonly",
                at="HEAD",
                ensure_view_id=expected_view_id,
            ),
            request("view-status", view=None, ensure_view_id=expected_view_id),
        )
        for value in excluded:
            with self.subTest(command=value["command"]):
                with self.assertRaises(GitViewspaceError) as caught:
                    validate_request(value)
                self.assertEqual(caught.exception.code, "INVALID_REQUEST")

    def test_cli_exposes_view_identity_guard_on_existing_view_verbs_only(self):
        view_id = "1" * 32
        guarded_commands = (
            ("view", "init", "overlay", "docs", "--branch", "main"),
            ("view", "start", "docs"),
            ("view", "stop", "docs"),
            ("view", "sync", "docs"),
            ("view", "stage", "docs"),
            ("view", "commit", "docs", "--message", "message"),
            ("view", "advance", "docs", "--message", "message"),
            ("view", "status", "docs"),
            ("view", "switch", "docs", "--to", "HEAD"),
            ("view", "clean", "docs"),
            ("view", "deinit", "overlay", "docs"),
            ("view", "destroy", "docs"),
        )
        for command in guarded_commands:
            with self.subTest(command=command):
                parsed = cli_parser().parse_args(
                    ("--root", str(self.root), *command, f"--ensure-view-id={view_id}")
                )
                self.assertEqual(parsed.ensure_view_id, view_id)

        for command in (
            ("view", "init", "readonly", "docs", "--at", "HEAD"),
            ("view", "init-and-start", "readonly", "docs", "--at", "HEAD"),
        ):
            with self.subTest(command=command), self.assertRaises(UsageError):
                cli_parser().parse_args(
                    ("--root", str(self.root), *command, f"--ensure-view-id={view_id}")
                )

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = cli_run(
                (
                    "--root",
                    str(self.root),
                    "--json",
                    "view",
                    "status",
                    f"--ensure-view-id={view_id}",
                )
            )
        self.assertEqual(result, 2)
        self.assertEqual(json.loads(output.getvalue())["error"]["code"], "INVALID_ARGUMENT")

    def test_offline_status_and_stop_are_read_only_and_idempotent(self):
        observed = status(self.root)
        self.assertTrue(observed["healthy"])
        self.assertFalse(observed["daemon"]["running"])
        result = stop(self.root)
        self.assertTrue(result["idempotent"])
        self.assertFalse((self.root / "run/runtime.json").exists())

    def test_offline_stop_rejects_runtime_residue(self):
        (self.root / "run/unexplained").write_text("residue\n", encoding="utf-8")
        observed = status(self.root)
        self.assertFalse(observed["healthy"])
        with self.assertRaises(GitViewspaceError) as caught:
            stop(self.root)
        self.assertEqual(caught.exception.code, "DAEMON_NOT_RUNNING")

    def test_cli_reports_offline_status_as_json(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            exit_status = cli_run(("--root", str(self.root), "--json", "status"))
        self.assertEqual(exit_status, 0)
        payload = json.loads(output.getvalue())
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["daemon"]["running"])

    def test_runtime_schema_is_strict(self):
        value = {
            "format": RUNTIME_FORMAT,
            "schema_version": SCHEMA_VERSION,
            "root_uuid": "root-uuid",
            "daemon": {"pid": 10, "start_time_ticks": 20},
            "viewfs": {
                "attempt_token": "attempt",
                "source": "GitViewspaceFS:root-uuid:attempt",
                "supervisor_pid": 30,
                "supervisor_start_time_ticks": 40,
            },
            "restart_count": 0,
            "ready": True,
        }
        self.assertIs(validate_runtime(value), value)
        value["extra"] = True
        with self.assertRaises(GitViewspaceError):
            validate_runtime(value)

    def _started_fake_daemon(self):
        context = self.context()
        daemon = Daemon(context, viewfs_factory=_FakeViewFS)

        def mount_observation(_path, _observed=None):
            if daemon.viewfs.running:
                return self.mounted(daemon.viewfs.source)
            return None

        patcher = mock.patch("git_viewspace.daemon.mount_at", side_effect=mount_observation)
        patcher.start()
        self.addCleanup(patcher.stop)
        with mock.patch.object(daemon, "_bind_socket", side_effect=lambda: setattr(daemon, "server", _FakeServer())):
            daemon.start()
        return context, daemon

    @staticmethod
    def _daemon_request(daemon, command, **arguments):
        stream = _MemoryStream(json.dumps(request(command, **arguments)).encode("utf-8") + b"\n")
        daemon._serve_connection(stream)
        return decode_message(bytes(stream.sent).removesuffix(b"\n"), "response")

    def test_daemon_protocol_status_supervision_and_stop(self):
        context, daemon = self._started_fake_daemon()
        observed = self._daemon_request(daemon, "status")
        self.assertTrue(observed["healthy"])
        self.assertEqual(observed["viewfs"]["restart_count"], 0)

        daemon.viewfs.running = False
        daemon.supervise()
        observed = self._daemon_request(daemon, "status")
        self.assertEqual(observed["viewfs"]["restart_count"], 1)
        self.assertTrue(observed["viewfs"]["healthy"])

        result = self._daemon_request(daemon, "stop")
        self.assertTrue(result["stopped"])
        self.assertFalse(context.socket_path.exists())
        self.assertFalse(context.runtime_path.exists())

    def test_start_publishes_nonready_runtime_before_restoring_views(self):
        context = self.context()
        daemon = Daemon(context, viewfs_factory=_FakeViewFS)
        observed = []

        def restore():
            runtime = json.loads(context.runtime_path.read_text(encoding="utf-8"))
            observed.append(runtime)

        with (
            mock.patch.object(daemon, "_restore_views", side_effect=restore),
            mock.patch.object(
                daemon,
                "_bind_socket",
                side_effect=lambda: setattr(daemon, "server", _FakeServer()),
            ),
        ):
            final = daemon.start()
        self.assertEqual(len(observed), 1)
        self.assertFalse(observed[0]["ready"])
        self.assertEqual(observed[0]["viewfs"]["source"], daemon.viewfs.source)
        self.assertTrue(final["ready"])

    def test_failed_start_closes_bound_socket_and_removes_runtime(self):
        context = self.context()
        daemon = Daemon(context, viewfs_factory=_FakeViewFS)
        server = _FakeServer()
        real_write_runtime = daemon._write_runtime

        def write_runtime(*, ready):
            if ready:
                raise GitViewspaceError("INJECTED_FAILURE", "runtime publication failed")
            return real_write_runtime(ready=False)

        def bind():
            context.socket_path.write_text("bound placeholder\n", encoding="utf-8")
            daemon.server = server

        with (
            mock.patch.object(daemon, "_bind_socket", side_effect=bind),
            mock.patch.object(daemon, "_write_runtime", side_effect=write_runtime),
            self.assertRaises(GitViewspaceError),
        ):
            daemon.start()
        self.assertTrue(server.closed)
        self.assertIsNone(daemon.server)
        self.assertFalse(context.socket_path.exists())
        self.assertFalse(context.runtime_path.exists())
        self.assertFalse(daemon.viewfs.running)

    def test_failed_start_preserves_runtime_when_mount_cleanup_fails(self):
        context = self.context()
        daemon = Daemon(
            context,
            viewfs_factory=_FakeViewFS,
            mount_factory=_FakeMounts,
        )

        def fail_restore():
            raise GitViewspaceError("VIEW_MOUNT_FAILED", "injected restore failure")

        with (
            mock.patch.object(daemon, "_restore_views", side_effect=fail_restore),
            mock.patch.object(
                daemon.mounts,
                "cleanup_all",
                side_effect=GitViewspaceError("VIEW_UNMOUNT_FAILED", "injected cleanup failure"),
            ),
            self.assertRaises(GitViewspaceError) as caught,
        ):
            daemon.start()
        self.assertEqual(caught.exception.code, "VIEW_UNMOUNT_FAILED")
        runtime = json.loads(context.runtime_path.read_text(encoding="utf-8"))
        self.assertFalse(runtime["ready"])
        self.assertEqual(runtime["viewfs"]["source"], daemon.viewfs.source)

    def test_viewfs_recovery_rebinds_nonready_runtime_before_restore(self):
        context, daemon = self._started_fake_daemon()
        previous_source = daemon.viewfs.source
        daemon.viewfs.running = False
        observed = []

        def restore():
            runtime = json.loads(context.runtime_path.read_text(encoding="utf-8"))
            observed.append(runtime)

        with mock.patch.object(daemon, "_restore_views", side_effect=restore):
            daemon._recover_viewfs()
        self.assertEqual(len(observed), 1)
        self.assertFalse(observed[0]["ready"])
        self.assertNotEqual(observed[0]["viewfs"]["source"], previous_source)
        self.assertEqual(observed[0]["viewfs"]["source"], daemon.viewfs.source)
        self.assertTrue(json.loads(context.runtime_path.read_text(encoding="utf-8"))["ready"])

    def test_shutdown_continues_when_nonready_runtime_write_fails(self):
        context, daemon = self._started_fake_daemon()
        server = daemon.server
        with mock.patch.object(
            daemon,
            "_write_runtime",
            side_effect=OSError("injected runtime write failure"),
        ):
            daemon._shutdown()
        self.assertTrue(server.closed)
        self.assertIsNone(daemon.server)
        self.assertFalse(daemon.viewfs.running)
        self.assertFalse(context.socket_path.exists())
        self.assertFalse(context.runtime_path.exists())

    def test_daemon_rejects_durable_state_that_differs_from_memory(self):
        context, daemon = self._started_fake_daemon()
        changed = json.loads(context.state_path.read_text(encoding="utf-8"))
        changed["views"]["external"] = {"view_id": "1" * 32}
        atomic_json(context.state_path, changed)
        response = self._daemon_request(daemon, "status")
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "STALE_DURABLE_STATE")

    def test_background_start_is_idempotent_for_running_daemon(self):
        context, daemon = self._started_fake_daemon()
        running = daemon.status()
        with mock.patch("git_viewspace.service._running_status", return_value=running):
            result = start_background(self.root)
        self.assertTrue(result["idempotent"])
        self.assertEqual(result["daemon"]["pid"], os.getpid())
        self.assertEqual(context.root_uuid, result["root_uuid"])

    def test_online_view_command_uses_one_rpc_without_full_root_reload(self):
        context = self.context()
        lightweight = mock.Mock(
            root=context.root,
            root_uuid=context.root_uuid,
            socket_path=context.socket_path,
        )
        response = {
            "ok": True,
            "command": "view start",
            "root": str(context.root),
            "root_uuid": context.root_uuid,
            "view": {"name": "docs"},
            "idempotent": False,
        }
        with (
            mock.patch("git_viewspace.service.OnlineRoot.load", return_value=lightweight) as load,
            mock.patch("git_viewspace.service.RootContext.load", side_effect=AssertionError("full reload")),
            mock.patch("git_viewspace.service.call", return_value=response) as rpc,
        ):
            self.assertEqual(service.view_start(self.root, "docs"), response)
        load.assert_called_once_with(self.root, view="docs", allow_missing_view=False)
        rpc.assert_called_once_with(context.socket_path, "view-start", timeout=30.0, view="docs")

    def test_view_destroy_threads_optional_expected_identity(self):
        context = self.context()
        lightweight = mock.Mock(
            root=context.root,
            root_uuid=context.root_uuid,
            socket_path=context.socket_path,
        )
        response = {
            "ok": True,
            "command": "view destroy",
            "root": str(context.root),
            "root_uuid": context.root_uuid,
            "name": "docs",
            "view_id": "1" * 32,
            "destroyed": True,
            "idempotent": False,
        }
        with (
            mock.patch("git_viewspace.service.OnlineRoot.load", return_value=lightweight),
            mock.patch("git_viewspace.service.call", return_value=response) as rpc,
        ):
            self.assertEqual(
                service.view_destroy(
                    self.root,
                    "docs",
                    ensure_view_id="1" * 32,
                ),
                response,
            )
        rpc.assert_called_once_with(
            context.socket_path,
            "view-destroy",
            timeout=300.0,
            view="docs",
            discard=False,
            ensure_view_id="1" * 32,
        )

        with (
            mock.patch("git_viewspace.service.OnlineRoot.load", return_value=lightweight),
            mock.patch("git_viewspace.service.call", return_value=response) as rpc,
        ):
            service.view_destroy(self.root, "docs")
        rpc.assert_called_once_with(
            context.socket_path,
            "view-destroy",
            timeout=300.0,
            view="docs",
            discard=False,
        )

    def test_existing_view_service_threads_optional_identity_without_null(self):
        context = self.context()
        lightweight = mock.Mock(
            root=context.root,
            root_uuid=context.root_uuid,
            socket_path=context.socket_path,
        )
        response = {
            "ok": True,
            "command": "view start",
            "root": str(context.root),
            "root_uuid": context.root_uuid,
            "view": {"name": "docs", "view_id": "1" * 32},
            "idempotent": False,
        }
        with (
            mock.patch("git_viewspace.service.OnlineRoot.load", return_value=lightweight),
            mock.patch("git_viewspace.service.call", return_value=response) as rpc,
        ):
            service.view_start(self.root, "docs", ensure_view_id="1" * 32)
        rpc.assert_called_once_with(
            context.socket_path,
            "view-start",
            timeout=30.0,
            view="docs",
            ensure_view_id="1" * 32,
        )

        with (
            mock.patch("git_viewspace.service.OnlineRoot.load", return_value=lightweight),
            mock.patch("git_viewspace.service.call", return_value=response) as rpc,
        ):
            service.view_start(self.root, "docs")
        rpc.assert_called_once_with(
            context.socket_path,
            "view-start",
            timeout=30.0,
            view="docs",
        )

    def test_background_start_uses_readiness_handshake(self):
        context = self.context()
        running = {
            "ok": True,
            "command": "status",
            "daemon": {"running": True, "pid": 123, "start_time_ticks": 456},
            "viewfs": {"healthy": True},
        }
        process = _FakeProcess()

        def popen(command, **kwargs):
            self.assertEqual(kwargs["cwd"], "/")
            self.assertIn(str(Path(__file__).resolve().parents[1]), kwargs["env"]["PYTHONPATH"])
            ready_fd = int(command[command.index("--ready-fd") + 1])
            os.write(ready_fd, b'{"ok":true}\n')
            return process

        with mock.patch("git_viewspace.service._running_status", side_effect=(None, running)):
            result = start_background(self.root, popen=popen)
        self.assertFalse(result["idempotent"])
        self.assertEqual(result["daemon"]["pid"], 123)
        self.assertEqual(result["root_uuid"], context.root_uuid)

    def test_reconcile_removes_only_runtime_bound_stale_mount(self):
        context = self.context()
        prior = _FakeViewFS(context)
        prior.start()
        runtime = {
            "format": RUNTIME_FORMAT,
            "schema_version": SCHEMA_VERSION,
            "root_uuid": context.root_uuid,
            "daemon": {"pid": 101, "start_time_ticks": 202},
            "viewfs": prior.runtime(),
            "restart_count": 0,
            "ready": True,
        }
        atomic_json(context.runtime_path, runtime)
        daemon = Daemon(context, viewfs_factory=_FakeViewFS)
        mounted = self.mounted(runtime["viewfs"]["source"])
        with (
            mock.patch("git_viewspace.daemon.mount_at", return_value=mounted),
            mock.patch("git_viewspace.daemon.process_matches", return_value=False),
        ):
            daemon.reconcile()
        self.assertEqual(daemon.viewfs.cleaned_sources, [runtime["viewfs"]["source"]])
        self.assertFalse(context.runtime_path.exists())

    def test_viewfs_launch_profile_and_cleanup_commands(self):
        context = self.context()
        process = _FakeProcess()
        state = {"mounted": False, "source": None, "command": None, "unmount": None}

        def popen(command, **_kwargs):
            state["command"] = tuple(command)
            state["source"] = f"GitViewspaceFS:{context.root_uuid}:{command[-1]}"
            state["mounted"] = True
            return process

        def mounted(_path):
            return self.mounted(state["source"]) if state["mounted"] else None

        def validated(_path, source):
            if state["mounted"] and source == state["source"]:
                return self.mounted(source)
            return None

        def run_unmount(command, **_kwargs):
            state["unmount"] = tuple(command)
            state["mounted"] = False
            return subprocess.CompletedProcess(command, 0, b"", b"")

        controller = ViewFS(
            context,
            executable="/bin/true",
            sudo="/bin/true",
            umount="/bin/true",
            popen=popen,
            run=run_unmount,
            timeout=0.25,
        )
        with (
            mock.patch("git_viewspace.viewfs.mount_at", side_effect=mounted),
            mock.patch("git_viewspace.viewfs.validate_private_mount", side_effect=validated),
            mock.patch("git_viewspace.viewfs.process_start_time_ticks", return_value=987654) as identity,
            mock.patch.object(controller, "_validate_exposed_root"),
        ):
            runtime = controller.start()
            command = state["command"]
            self.assertEqual(command[:4], ("/usr/bin/true", "-n", "--", "/usr/bin/true"))
            self.assertIn("--workload-uid", command)
            self.assertIn("--workload-gid", command)
            self.assertIn("--root-uuid", command)
            self.assertIn("--attempt-token", command)
            self.assertEqual(runtime["source"], state["source"])
            self.assertEqual(runtime["supervisor_start_time_ticks"], 987654)
            self.assertEqual(controller.runtime()["supervisor_start_time_ticks"], 987654)
            identity.assert_called_once_with(process.pid)
            controller.stop()
        self.assertFalse(state["mounted"])
        self.assertEqual(state["unmount"][-2:], ("--", str(context.mountpoint)))

    def _started_view_daemon(self):
        context = self.context()
        commits = {"HEAD": context.repository_facts.head, "next": "1" * 40}
        daemon = Daemon(
            context,
            viewfs_factory=_FakeViewFS,
            mount_factory=_FakeMounts,
            git=_FakeGit(commits),
            filesystem=lambda _path: Mount(
                Path("/"),
                "/",
                "ext4",
                "/dev/test",
                frozenset({"rw"}),
                frozenset({"rw"}),
            ),
        )
        with mock.patch.object(
            daemon,
            "_bind_socket",
            side_effect=lambda: setattr(daemon, "server", _FakeServer()),
        ):
            daemon.start()
        return context, daemon

    def _started_real_git_view_daemon(self):
        context = self.context()
        daemon = Daemon(
            context,
            viewfs_factory=_FakeViewFS,
            mount_factory=_FakeMounts,
            git=Git(),
            filesystem=lambda _path: Mount(
                Path("/"),
                "/",
                "ext4",
                "/dev/test",
                frozenset({"rw"}),
                frozenset({"rw"}),
            ),
        )
        with mock.patch.object(
            daemon,
            "_bind_socket",
            side_effect=lambda: setattr(daemon, "server", _FakeServer()),
        ):
            daemon.start()
        return context, daemon

    def test_readonly_view_initialization_is_durable_and_idempotent(self):
        context, daemon = self._started_view_daemon()
        created = daemon.view_init_readonly("docs", "HEAD")
        self.assertFalse(created["idempotent"])
        view = created["view"]
        self.assertEqual(view["commit"], context.repository_facts.head)
        self.assertEqual(view["actual"], "stopped")
        self.assertTrue((self.root / "views/docs/config/view.json").is_file())
        self.assertTrue((self.root / "views/docs/state/state.json").is_file())
        self.assertEqual(
            json.loads((self.root / "config/viewfs.json").read_text(encoding="utf-8")),
            {view["alias"]: {"commit": context.repository_facts.head}},
        )
        self.assertTrue(daemon.view_init_readonly("docs", "HEAD")["idempotent"])
        with self.assertRaises(GitViewspaceError) as caught:
            daemon.view_init_readonly("docs", "next")
        self.assertEqual(caught.exception.code, "VIEW_EXISTS")
        reloaded = RootContext.load(self.root)
        self.assertEqual(reloaded.views["docs"].commit, context.repository_facts.head)

    def test_cli_dispatches_nested_readonly_view_command(self):
        payload = {
            "ok": True,
            "command": "view init readonly",
            "root": str(self.root),
            "view": {"name": "docs"},
            "idempotent": False,
        }
        output = io.StringIO()
        with (
            mock.patch("git_viewspace.cli.view_init_readonly", return_value=payload) as operation,
            contextlib.redirect_stdout(output),
        ):
            result = cli_run(
                (
                    "--root",
                    str(self.root),
                    "--json",
                    "view",
                    "init",
                    "readonly",
                    "docs",
                    "--at",
                    "HEAD",
                )
            )
        self.assertEqual(result, 0)
        operation.assert_called_once_with(str(self.root), "docs", "HEAD")
        self.assertEqual(json.loads(output.getvalue()), payload)

    def test_cli_dispatches_overlay_init_clean_and_deinit(self):
        overlay_payload = {
            "ok": True,
            "command": "view init overlay",
            "root": str(self.root),
            "view": {"name": "work"},
            "idempotent": False,
        }
        with (
            mock.patch("git_viewspace.cli.view_init_overlay", return_value=overlay_payload) as operation,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(
                cli_run(
                    (
                        "--root",
                        str(self.root),
                        "--json",
                        "view",
                        "init",
                        "overlay",
                        "work",
                        "--branch",
                        "main",
                    )
                ),
                0,
            )
        operation.assert_called_once_with(str(self.root), "work", "main")
        with (
            mock.patch("git_viewspace.cli.view_clean", return_value={**overlay_payload, "command": "view clean"}) as clean,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(
                cli_run(("--root", str(self.root), "--json", "view", "clean", "work")),
                0,
            )
        clean.assert_called_once_with(str(self.root), "work")
        with (
            mock.patch(
                "git_viewspace.cli.view_deinit_overlay",
                return_value={**overlay_payload, "command": "view deinit overlay"},
            ) as deinit,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(
                cli_run(
                    (
                        "--root",
                        str(self.root),
                        "--json",
                        "view",
                        "deinit",
                        "overlay",
                        "work",
                    )
                ),
                0,
            )
        deinit.assert_called_once_with(str(self.root), "work")

    def test_cli_dispatches_view_sync(self):
        payload = {
            "ok": True,
            "command": "view sync",
            "root": str(self.root),
            "view": {"name": "work"},
            "sync": {"manifest": ["created.txt"]},
            "idempotent": False,
        }
        with (
            mock.patch("git_viewspace.cli.view_sync", return_value=payload) as operation,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(
                cli_run(("--root", str(self.root), "--json", "view", "sync", "work")),
                0,
            )
        operation.assert_called_once_with(str(self.root), "work")

    def test_cli_dispatches_compound_view_commands(self):
        payload = {
            "ok": True,
            "command": "view init-and-start",
            "root": str(self.root),
            "view": {"name": "work"},
            "steps": [],
            "idempotent": False,
        }
        with (
            mock.patch("git_viewspace.cli.view_init_and_start", return_value=payload) as operation,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(
                cli_run(
                    (
                        "--root",
                        str(self.root),
                        "--json",
                        "view",
                        "init-and-start",
                        "overlay",
                        "work",
                        "--from-branch",
                        "main",
                        "--new-branch",
                        "runner-work",
                    )
                ),
                0,
            )
        operation.assert_called_once_with(
            str(self.root),
            "work",
            "overlay",
            from_branch="main",
            new_branch="runner-work",
        )

        advanced = {**payload, "command": "view advance"}
        with (
            mock.patch("git_viewspace.cli.view_advance", return_value=advanced) as operation,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(
                cli_run(
                    (
                        "--root",
                        str(self.root),
                        "--json",
                        "view",
                        "advance",
                        "work",
                        "--message",
                        "next",
                    )
                ),
                0,
            )
        operation.assert_called_once_with(str(self.root), "work", "next")

    def test_cli_dispatches_conditional_destroy_and_validates_view_id(self):
        view_id = "1" * 32
        payload = {
            "ok": True,
            "command": "view destroy",
            "root": str(self.root),
            "name": "-work",
            "view_id": view_id,
            "destroyed": True,
            "idempotent": False,
        }
        output = io.StringIO()
        with (
            mock.patch("git_viewspace.cli.view_destroy", return_value=payload) as operation,
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(
                cli_run(
                    (
                        "--root",
                        str(self.root),
                        "--json",
                        "view",
                        "destroy",
                        f"--ensure-view-id={view_id}",
                        "--discard",
                        "--",
                        "-work",
                    )
                ),
                0,
            )
        operation.assert_called_once_with(
            str(self.root),
            "-work",
            True,
            ensure_view_id=view_id,
        )
        self.assertEqual(json.loads(output.getvalue()), payload)

        with (
            mock.patch("git_viewspace.cli.view_destroy", return_value=payload) as operation,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(
                cli_run(
                    (
                        "--root",
                        str(self.root),
                        "--json",
                        "view",
                        "destroy",
                        "work",
                    )
                ),
                0,
            )
        operation.assert_called_once_with(str(self.root), "work", False)

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(
                cli_run(
                    (
                        "--root",
                        str(self.root),
                        "--json",
                        "view",
                        "destroy",
                        "work",
                        "--ensure-view-id=ABC",
                    )
                ),
                2,
            )
        invalid = json.loads(output.getvalue())
        self.assertEqual(invalid["error"]["code"], "INVALID_ARGUMENT")
        self.assertIn("32 lowercase hexadecimal characters", invalid["error"]["message"])

    def test_sync_report_and_durable_schema_are_strict(self):
        report = _FakeSync().report
        self.assertEqual(validate_report(report, "inspect")["git_pathspecs"], report["git_pathspecs"])
        large = dict(report, git_pathspecs=["dataset/" + "x" * 100 + f"{i:06d}" for i in range(80000)])
        payload = json.dumps(large).encode()
        self.assertGreater(len(payload), 8 * 1024 * 1024)
        result = subprocess.CompletedProcess([], 0, stdout=payload, stderr=b"")
        helper = OverlaySync(run=mock.Mock(return_value=result))
        self.assertEqual(helper.inspect(self.root), large)
        with self.assertRaises(GitViewspaceError):
            validate_report({**report, "git_pathspecs": ["../escape"]}, "inspect")
        record = {
            "format": SYNC_FORMAT,
            "schema_version": SCHEMA_VERSION,
            "root_uuid": "root-uuid",
            "view_id": "1" * 32,
            "sync_id": "0" * 32,
            "generation_id": "3" * 32,
            "branch": "main",
            "base_commit": "4" * 40,
            "upper_fingerprint": "5" * 64,
            "manifest": ["created.txt", "deleted.txt"],
            "manifest_sha256": sync_manifest_digest(["created.txt", "deleted.txt"]),
            "base_tree": "7" * 40,
            "counts": report["counts"],
        }
        record["sync_id"] = sync_identifier(record)
        self.assertEqual(validate_sync(record), record)
        with self.assertRaises(GitViewspaceError):
            validate_sync({**record, "manifest": ["created.txt", "created.txt"]})

    def test_completed_init_remains_idempotent_after_readonly_view_creation(self):
        _context, daemon = self._started_view_daemon()
        daemon.view_init_readonly("docs", "HEAD")
        daemon._shutdown()
        result = Initializer().initialize(
            InitRequest(
                root=self.root,
                git_name="Managed User",
                git_email="managed@example.invalid",
                source_mode="empty",
            )
        )
        self.assertTrue(result.idempotent)

    def test_readonly_view_start_stop_switch_status_and_destroy(self):
        _context, daemon = self._started_view_daemon()
        daemon.view_init_readonly("docs", "HEAD")
        started = daemon.view_start("docs")
        self.assertFalse(started["idempotent"])
        self.assertEqual(started["view"]["actual"], "readonly")
        self.assertTrue(daemon.view_start("docs")["idempotent"])
        with self.assertRaises(GitViewspaceError) as caught:
            daemon.view_switch("docs", "next")
        self.assertEqual(caught.exception.code, "VIEW_STARTED")

        listed = daemon.view_status(None)
        self.assertEqual([view["name"] for view in listed["views"]], ["docs"])
        stopped = daemon.view_stop("docs")
        self.assertFalse(stopped["idempotent"])
        self.assertTrue(daemon.view_stop("docs")["idempotent"])
        switched = daemon.view_switch("docs", "next")
        self.assertEqual(switched["view"]["commit"], "1" * 40)
        self.assertFalse(switched["idempotent"])
        self.assertTrue(daemon.view_switch("docs", "next")["idempotent"])
        view_id = daemon.store.get("docs").view_id
        destroyed = daemon.view_destroy("docs", False)
        self.assertTrue(destroyed["destroyed"])
        self.assertEqual(destroyed["view_id"], view_id)
        self.assertFalse((self.root / "views/docs").exists())
        self.assertEqual(json.loads((self.root / "config/viewfs.json").read_text()), {})

    def test_destroy_identity_mismatch_is_side_effect_free(self):
        _context, daemon = self._started_view_daemon()
        daemon.view_init_readonly("docs", "HEAD")
        view = daemon.store.get("docs")
        expected_path = view.path
        wrong_view_id = ("0" if view.view_id[0] != "0" else "1") + view.view_id[1:]
        with (
            mock.patch.object(daemon.mounts, "observe", wraps=daemon.mounts.observe) as observe,
            mock.patch.object(daemon.store, "destroy", wraps=daemon.store.destroy) as destroy,
            self.assertRaises(GitViewspaceError) as caught,
        ):
            daemon.view_destroy("docs", False, ensure_view_id=wrong_view_id)
        self.assertEqual(caught.exception.code, "VIEW_ID_MISMATCH")
        self.assertIn(view.view_id, caught.exception.message)
        self.assertIn(wrong_view_id, caught.exception.message)
        observe.assert_not_called()
        destroy.assert_not_called()
        self.assertTrue(expected_path.is_dir())
        self.assertEqual(daemon.store.get("docs").view_id, view.view_id)

        destroyed = daemon.view_destroy(
            "docs",
            False,
            ensure_view_id=view.view_id,
        )
        self.assertEqual(destroyed["view_id"], view.view_id)

    def test_destroy_protocol_reports_typed_identity_mismatch_without_effect(self):
        _context, daemon = self._started_view_daemon()
        daemon.view_init_readonly("docs", "HEAD")
        view = daemon.store.get("docs")
        wrong_view_id = ("0" if view.view_id[0] != "0" else "1") + view.view_id[1:]
        mismatch = self._daemon_request(
            daemon,
            "view-destroy",
            view="docs",
            discard=False,
            ensure_view_id=wrong_view_id,
        )
        self.assertFalse(mismatch["ok"])
        self.assertEqual(mismatch["error"]["code"], "VIEW_ID_MISMATCH")
        self.assertTrue(view.path.is_dir())

        destroyed = self._daemon_request(
            daemon,
            "view-destroy",
            view="docs",
            discard=False,
            ensure_view_id=view.view_id,
        )
        self.assertTrue(destroyed["ok"])
        self.assertEqual(destroyed["view_id"], view.view_id)

    def test_identity_guard_rejects_every_existing_view_command_before_dispatch(self):
        _context, daemon = self._started_view_daemon()
        daemon.view_init_readonly("docs", "HEAD")
        actual_view_id = daemon.store.get("docs").view_id
        wrong_view_id = ("0" if actual_view_id[0] != "0" else "1") + actual_view_id[1:]
        methods = {
            "view-init-overlay": "view_init_overlay",
            "view-start": "view_start",
            "view-stop": "view_stop",
            "view-sync": "view_sync",
            "view-stage": "view_stage",
            "view-commit": "view_commit",
            "view-advance": "view_advance",
            "view-status": "view_status",
            "view-switch": "view_switch",
            "view-clean": "view_clean",
            "view-deinit-overlay": "view_deinit_overlay",
            "view-destroy": "view_destroy",
        }
        for command, arguments in self.guarded_requests().items():
            with self.subTest(command=command), mock.patch.object(
                daemon,
                methods[command],
                side_effect=AssertionError("guarded command was dispatched"),
            ) as operation:
                response = self._daemon_request(
                    daemon,
                    command,
                    **arguments,
                    ensure_view_id=wrong_view_id,
                )
                self.assertFalse(response["ok"])
                self.assertEqual(response["error"]["code"], "VIEW_ID_MISMATCH")
                self.assertIn(actual_view_id, response["error"]["message"])
                self.assertIn(wrong_view_id, response["error"]["message"])
                operation.assert_not_called()

    def test_recreated_logical_name_rejects_stale_view_identity(self):
        _context, daemon = self._started_view_daemon()
        daemon.view_init_readonly("docs", "HEAD")
        stale_view_id = daemon.store.get("docs").view_id
        daemon.view_destroy("docs", False)
        daemon.view_init_readonly("docs", "HEAD")
        replacement = daemon.store.get("docs")
        self.assertNotEqual(replacement.view_id, stale_view_id)

        with mock.patch.object(
            daemon,
            "view_start",
            side_effect=AssertionError("stale handle reached view start"),
        ) as start:
            response = self._daemon_request(
                daemon,
                "view-start",
                view="docs",
                ensure_view_id=stale_view_id,
            )
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "VIEW_ID_MISMATCH")
        start.assert_not_called()
        self.assertFalse(replacement.desired_started)
        self.assertNotIn("docs", daemon.mounts.mounted)

    def test_offline_named_status_enforces_view_identity_without_daemon(self):
        _context, daemon = self._started_view_daemon()
        daemon.view_init_readonly("docs", "HEAD")
        view_id = daemon.store.get("docs").view_id
        daemon._shutdown()

        observed = service.view_status(self.root, "docs", ensure_view_id=view_id)
        self.assertEqual(observed["view"]["view_id"], view_id)
        wrong_view_id = ("0" if view_id[0] != "0" else "1") + view_id[1:]
        with self.assertRaises(GitViewspaceError) as caught:
            service.view_status(self.root, "docs", ensure_view_id=wrong_view_id)
        self.assertEqual(caught.exception.code, "VIEW_ID_MISMATCH")
        with self.assertRaises(GitViewspaceError) as caught:
            service.view_status(self.root, ensure_view_id=view_id)
        self.assertEqual(caught.exception.code, "INVALID_ARGUMENT")
        with self.assertRaises(GitViewspaceError) as caught:
            service.view_status(self.root, "docs", ensure_view_id="ABC")
        self.assertEqual(caught.exception.code, "INVALID_ARGUMENT")

    def test_desired_readonly_view_is_restored_by_new_daemon(self):
        _context, daemon = self._started_view_daemon()
        daemon.view_init_readonly("docs", "HEAD")
        daemon.view_start("docs")
        daemon.mounts.mounted.clear()

        context = RootContext.load(self.root)
        replacement = Daemon(
            context,
            viewfs_factory=_FakeViewFS,
            mount_factory=_FakeMounts,
            git=_FakeGit({"HEAD": context.repository_facts.head}),
        )
        with mock.patch.object(
            replacement,
            "_bind_socket",
            side_effect=lambda: setattr(replacement, "server", _FakeServer()),
        ):
            replacement.start()
        self.assertEqual(replacement.mounts.mounted, {"docs"})
        self.assertEqual(replacement.view_status("docs")["view"]["actual"], "readonly")
        replacement.mounts.mounted.clear()
        replacement.supervise()
        self.assertEqual(replacement.mounts.mounted, {"docs"})

    def test_public_mount_uses_bind_remount_and_orderly_unmount(self):
        context, daemon = self._started_view_daemon()
        view = daemon.store.initialize("docs", context.repository_facts.head, lambda _view: None)[0]
        alias_path = context.mountpoint / "views" / view.alias
        alias_path.parent.mkdir(mode=0o755)
        alias_path.mkdir(mode=0o755)
        state = {"mounted": False, "commands": []}
        source = "GitViewspaceFS:root:attempt"

        def observed(_path):
            if not state["mounted"]:
                return None
            return Mount(
                view.mountpoint,
                f"/views/{view.alias}",
                "fuse",
                source,
                frozenset({"ro", "nosuid", "nodev"}),
                frozenset({"ro"}),
            )

        def run_command(command, **_kwargs):
            state["commands"].append(tuple(command))
            if "remount,bind,ro,nosuid,nodev" in command:
                state["mounted"] = True
            elif "--bind" not in command:
                state["mounted"] = False
            return subprocess.CompletedProcess(command, 0, b"", b"")

        mounts = ReadonlyMounts(
            context,
            sudo="/bin/true",
            mount="/bin/true",
            umount="/bin/true",
            run=run_command,
        )
        with mock.patch("git_viewspace.views.mount_at", side_effect=observed):
            self.assertFalse(mounts.start(view, source))
            self.assertFalse(mounts.stop(view, source))
        self.assertIn("--bind", state["commands"][0])
        self.assertIn("remount,bind,ro,nosuid,nodev", state["commands"][1])
        self.assertEqual(state["commands"][2][-2:], ("--", str(view.mountpoint)))

    def test_orderly_unmount_retries_after_transient_failure(self):
        context = self.context()
        state = {"mounted": True, "attempts": 0}

        def run_unmount(command, **_kwargs):
            state["attempts"] += 1
            if state["attempts"] == 1:
                return subprocess.CompletedProcess(command, 32, b"", b"target is busy")
            state["mounted"] = False
            return subprocess.CompletedProcess(command, 0, b"", b"")

        mounts = ReadonlyMounts(
            context,
            sudo="/bin/true",
            umount="/bin/true",
            run=run_unmount,
        )
        with (
            mock.patch(
                "git_viewspace.views.mount_at",
                side_effect=lambda _path: self.mounted("source") if state["mounted"] else None,
            ),
            mock.patch("git_viewspace.views.time.sleep") as sleep,
        ):
            mounts._unmount(context.root / "view", retry=True)
        self.assertEqual(state["attempts"], 2)
        sleep.assert_called_once_with(5.0)

    def test_orderly_unmount_accepts_failed_command_if_mount_is_gone(self):
        context = self.context()
        attempts = []

        def run_unmount(command, **_kwargs):
            attempts.append(tuple(command))
            return subprocess.CompletedProcess(command, 32, b"", b"not mounted")

        mounts = ReadonlyMounts(
            context,
            sudo="/bin/true",
            umount="/bin/true",
            run=run_unmount,
        )
        with (
            mock.patch("git_viewspace.views.mount_at", return_value=None),
            mock.patch("git_viewspace.views.time.sleep") as sleep,
        ):
            mounts._unmount(context.root / "view", retry=True)
        self.assertEqual(len(attempts), 1)
        sleep.assert_not_called()

    def test_orderly_unmount_bounds_persistent_failure(self):
        context = self.context()
        attempts = []

        def run_unmount(command, **_kwargs):
            attempts.append(tuple(command))
            return subprocess.CompletedProcess(command, 32, b"", b"target is busy")

        mounts = ReadonlyMounts(
            context,
            sudo="/bin/true",
            umount="/bin/true",
            run=run_unmount,
        )
        with (
            mock.patch("git_viewspace.views.mount_at", return_value=self.mounted("source")),
            mock.patch("git_viewspace.views.time.sleep") as sleep,
            self.assertRaises(GitViewspaceError) as caught,
        ):
            mounts._unmount(context.root / "view", retry=True)
        self.assertEqual(caught.exception.code, "VIEW_UNMOUNT_FAILED")
        self.assertEqual(len(attempts), 7)
        self.assertEqual(sleep.call_count, 6)

    def test_forceful_stop_uses_immediate_lazy_fallback_without_retry(self):
        context, daemon = self._started_view_daemon()
        view = daemon.store.initialize("docs", context.repository_facts.head, lambda _view: None)[0]
        source = "GitViewspaceFS:root:attempt"
        state = {"mounted": True, "attempts": []}

        def observed(_path):
            if not state["mounted"]:
                return None
            return Mount(
                view.mountpoint,
                f"/views/{view.alias}",
                "fuse",
                source,
                frozenset({"ro", "nosuid", "nodev"}),
                frozenset({"ro"}),
            )

        def run_unmount(command, **_kwargs):
            state["attempts"].append(tuple(command))
            if "-l" in command:
                state["mounted"] = False
                return subprocess.CompletedProcess(command, 0, b"", b"")
            return subprocess.CompletedProcess(command, 32, b"", b"target is busy")

        mounts = ReadonlyMounts(context, sudo="/bin/true", umount="/bin/true", run=run_unmount)
        with (
            mock.patch("git_viewspace.views.mount_at", side_effect=observed),
            mock.patch("git_viewspace.views.time.sleep") as sleep,
        ):
            self.assertFalse(mounts.stop(view, source, force=True))
        self.assertEqual(len(state["attempts"]), 2)
        self.assertNotIn("-l", state["attempts"][0])
        self.assertIn("-l", state["attempts"][1])
        sleep.assert_not_called()

    def test_overlay_initialization_generation_dirty_clean_and_deinit(self):
        context, daemon = self._started_view_daemon()
        daemon.view_init_readonly("work", "HEAD")
        initialized = daemon.view_init_overlay("work", "main")
        view = daemon.store.get("work")
        self.assertEqual(initialized["view"]["kind"], "overlay")
        self.assertEqual(initialized["view"]["branch"], "main")
        self.assertFalse(initialized["view"]["dirty"])
        self.assertEqual(view.overlay_base, context.repository_facts.head)
        self.assertTrue((view.generation_path / "generation.json").is_file())
        first_generation = view.generation_id

        (view.upper_path / "created.txt").write_text("created\n", encoding="utf-8")
        self.assertTrue(daemon.view_status("work")["view"]["dirty"])
        with self.assertRaises(GitViewspaceError) as caught:
            daemon.view_deinit_overlay("work")
        self.assertEqual(caught.exception.code, "VIEW_DIRTY")

        cleaned = daemon.view_clean("work")
        self.assertFalse(cleaned["idempotent"])
        self.assertFalse(cleaned["view"]["dirty"])
        self.assertNotEqual(cleaned["view"]["generation"], first_generation)
        self.assertFalse(view.generation_path.exists())
        deinitialized = daemon.view_deinit_overlay("work")
        self.assertEqual(deinitialized["view"]["kind"], "readonly")
        self.assertFalse((self.root / "views/work/overlay").exists())
        self.assertTrue(daemon.view_deinit_overlay("work")["idempotent"])

    def test_overlay_requires_matching_unclaimed_branch_and_stopped_view(self):
        context, daemon = self._started_view_daemon()
        daemon.view_init_readonly("one", "HEAD")
        daemon.view_init_readonly("two", "HEAD")
        daemon.view_init_overlay("one", "main")
        with self.assertRaises(GitViewspaceError) as caught:
            daemon.view_init_overlay("two", "main")
        self.assertEqual(caught.exception.code, "BRANCH_CLAIMED")

        daemon.git.branches["other"] = "1" * 40
        with self.assertRaises(GitViewspaceError) as caught:
            daemon.view_init_overlay("two", "other")
        self.assertEqual(caught.exception.code, "BRANCH_DIVERGED")
        daemon.view_start("two")
        with self.assertRaises(GitViewspaceError) as caught:
            daemon.view_init_overlay("two", "main")
        self.assertEqual(caught.exception.code, "VIEW_STARTED")
        self.assertEqual(context.repository_facts.head, daemon.store.get("one").overlay_base)

    def test_dirty_overlay_destroy_requires_discard(self):
        _context, daemon = self._started_view_daemon()
        daemon.view_init_readonly("work", "HEAD")
        daemon.view_init_overlay("work", "main")
        view = daemon.store.get("work")
        (view.upper_path / "change").write_text("change\n", encoding="utf-8")
        with self.assertRaises(GitViewspaceError) as caught:
            daemon.view_destroy("work", False)
        self.assertEqual(caught.exception.code, "VIEW_DIRTY")
        daemon.view_destroy("work", True)
        self.assertFalse(view.path.exists())

    def test_overlay_mount_uses_fixed_profile(self):
        context, daemon = self._started_view_daemon()
        daemon.view_init_readonly("work", "HEAD")
        daemon.view_init_overlay("work", "main")
        view = daemon.store.get("work")
        alias_path = context.mountpoint / "views" / view.alias
        alias_path.parent.mkdir(mode=0o755)
        alias_path.mkdir(mode=0o755)
        state = {"mounted": False, "commands": [], "redirect": {"redirect_dir=on"}}
        source = "GitViewspaceFS:root:attempt"

        def observed(_path):
            if not state["mounted"]:
                return None
            return Mount(
                view.mountpoint,
                "/",
                "overlay",
                "overlay",
                frozenset({"rw"}),
                frozenset(
                    {
                        "rw",
                        f"lowerdir={alias_path}",
                        f"upperdir={view.upper_path}",
                        f"workdir={view.work_path}",
                        "uuid=null",
                    } | state["redirect"]
                ),
            )

        def run_command(command, **_kwargs):
            state["commands"].append(tuple(command))
            if "-t" in command:
                state["mounted"] = True
            else:
                state["mounted"] = False
            return subprocess.CompletedProcess(command, 0, b"", b"")

        mounts = ReadonlyMounts(
            context,
            sudo="/bin/true",
            mount="/bin/true",
            umount="/bin/true",
            run=run_command,
        )
        with mock.patch("git_viewspace.views.mount_at", side_effect=observed):
            self.assertFalse(mounts.start(view, source))
            self.assertFalse(mounts.stop(view, source))
            state["redirect"] = set()
            self.assertFalse(mounts.start(view, source))
            self.assertFalse(mounts.stop(view, source))
        cleanup = state["commands"][0]
        self.assertEqual(cleanup[-1], str(view.upper_path))
        self.assertTrue(cleanup[-2].endswith("overlay_origin.py"))
        command = state["commands"][1]
        self.assertIn("overlay", command)
        options = command[command.index("-o") + 1]
        for option in (
            "redirect_dir=on",
            "metacopy=off",
            "index=off",
            "nfs_export=off",
            "xino=off",
            "uuid=null",
            "verity=off",
        ):
            self.assertIn(option, options.split(","))

    def test_view_sync_validates_replays_records_and_repeats(self):
        context, daemon = self._started_view_daemon()
        daemon.view_init_readonly("work", "HEAD")
        daemon.view_init_overlay("work", "main")
        helper = _FakeSync()
        daemon.sync = helper
        base = context.repository_facts.head
        daemon.git.prepare_sync_checkout = mock.Mock(return_value="b" * 40)

        first = daemon.view_sync("work")
        self.assertFalse(first["idempotent"])
        self.assertEqual(first["sync"]["manifest"], ["created.txt", "deleted.txt"])
        self.assertEqual(
            [call[0] for call in helper.calls],
            ["replay"],
        )
        reloaded = RootContext.load(self.root)
        self.assertEqual(reloaded.views["work"].sync, first["sync"])
        self.assertEqual(
            reloaded.manager_state["repository_operation"]["sync_id"],
            first["sync"]["sync_id"],
        )
        with self.assertRaises(GitViewspaceError) as caught:
            daemon.view_start("work")
        self.assertEqual(caught.exception.code, "SYNC_EXISTS")

        second = daemon.view_sync("work")
        self.assertTrue(second["idempotent"])
        self.assertEqual(second["sync"], first["sync"])
        self.assertEqual(daemon.git.prepare_sync_checkout.call_count, 2)
        cleaned = daemon.view_clean("work")
        self.assertIsNone(cleaned["view"]["sync"])
        self.assertIsNone(daemon.store.repository_operation)

    def test_view_sync_rejects_wrong_state_and_incomplete_manifest(self):
        _context, daemon = self._started_view_daemon()
        daemon.view_init_readonly("docs", "HEAD")
        with self.assertRaises(GitViewspaceError) as caught:
            daemon.view_sync("docs")
        self.assertEqual(caught.exception.code, "VIEW_NOT_WRITABLE")

        daemon.view_init_overlay("docs", "main")
        helper = _FakeSync()
        daemon.sync = helper
        daemon.git.prepare_sync_checkout = mock.Mock(return_value="b" * 40)
        daemon.view_start("docs")
        with self.assertRaises(GitViewspaceError) as caught:
            daemon.view_sync("docs")
        self.assertEqual(caught.exception.code, "VIEW_STARTED")

    def test_stage_commit_clean_and_switch_next_changed_transition(self):
        context, daemon = self._started_real_git_view_daemon()
        daemon.view_init_readonly("work", "HEAD")
        daemon.view_init_overlay("work", "main")
        view = daemon.store.get("work")
        (view.upper_path / "created.txt").write_text("created\n", encoding="utf-8")
        helper = _FakeSync(
            {
                **_FakeSync().report,
                "git_pathspecs": ["created.txt"],
                "whiteouts": [],
                "counts": {
                    "directories": 0,
                    "regulars": 1,
                    "symlinks": 0,
                    "whiteouts": 0,
                    "opaque": 0,
                    "origin": 0,
                    "impure": 0,
                },
            }
        )

        def replay(_upper, checkout):
            helper.calls.append(("replay", Path(_upper), Path(checkout)))
            (Path(checkout) / "created.txt").write_text("created\n", encoding="utf-8")
            return {**helper.report, "mode": "direct"}

        helper.replay = replay
        daemon.sync = helper
        sync = daemon.view_sync("work")["sync"]
        staged = daemon.view_stage("work")
        self.assertEqual(staged["stage"]["sync_id"], sync["sync_id"])
        self.assertEqual(staged["stage"]["checkout_tree"], staged["stage"]["index_tree"])
        self.assertTrue(daemon.view_stage("work")["idempotent"])
        self.assertEqual(RootContext.load(self.root).views["work"].stage, staged["stage"])

        committed = daemon.view_commit("work", "Managed transition")
        next_record = committed["next"]
        self.assertTrue(next_record["changed"])
        self.assertEqual(next_record["base_commit"], context.repository_facts.head)
        self.assertEqual(daemon.git.resolve_local_branch(context.repository, "main"), next_record["commit"])
        self.assertTrue(daemon.view_commit("work", "Managed transition")["idempotent"])
        self.assertEqual(RootContext.load(self.root).views["work"].next, next_record)

        old_generation = daemon.store.get("work").generation_id
        cleaned = daemon.view_clean("work")
        self.assertIsNone(cleaned["view"]["sync"])
        self.assertIsNone(cleaned["view"]["stage"])
        self.assertEqual(cleaned["view"]["next"]["next_id"], next_record["next_id"])
        self.assertNotEqual(cleaned["view"]["generation"], old_generation)
        self.assertTrue(daemon.view_clean("work")["idempotent"])

        switched = daemon.view_switch("work", use_next=True)
        self.assertEqual(switched["view"]["commit"], next_record["commit"])
        self.assertIsNone(switched["view"]["next"])
        self.assertIsNone(daemon.store.repository_operation)
        self.assertEqual(
            (context.repository / "created.txt").read_text(encoding="utf-8"),
            "created\n",
        )
        reloaded = RootContext.load(self.root)
        self.assertEqual(reloaded.views["work"].commit, next_record["commit"])

    def test_compound_writable_creation_reports_post_branch_partial_state(self):
        context, daemon = self._started_real_git_view_daemon()
        failure = GitViewspaceError("INJECTED_VIEW_FAILURE", "injected after branch creation")
        with mock.patch.object(daemon, "view_init_readonly", side_effect=failure):
            with self.assertRaises(GitViewspaceError) as raised:
                daemon.view_init_and_start(
                    "partial",
                    "overlay",
                    from_branch="main",
                    new_branch="runner-partial",
                )
        self.assertEqual(raised.exception.code, "VIEW_INIT_PARTIAL")
        self.assertEqual(
            raised.exception.partial_state,
            {
                "view_name": "partial",
                "parent_branch": "main",
                "branch": "runner-partial",
                "branch_commit": context.repository_facts.head,
                "view_created": False,
            },
        )
        self.assertIn("INJECTED_VIEW_FAILURE", raised.exception.detail)
        self.assertEqual(
            daemon.git.resolve_local_branch(context.repository, "runner-partial"),
            context.repository_facts.head,
        )
        self.assertNotIn("partial", daemon.store.views)

    def test_compound_writable_creation_rejects_dangling_symbolic_branch(self):
        context, daemon = self._started_real_git_view_daemon()
        subprocess.run(
            (
                "git",
                "symbolic-ref",
                "refs/heads/runner-dangling",
                "refs/heads/missing-target",
            ),
            cwd=context.repository,
            check=True,
        )
        with self.assertRaises(GitViewspaceError) as raised:
            daemon.view_init_and_start(
                "dangling",
                "overlay",
                from_branch="main",
                new_branch="runner-dangling",
            )
        self.assertEqual(raised.exception.code, "BRANCH_EXISTS")
        self.assertNotIn("dangling", daemon.store.views)

    def test_branch_merge_advances_only_parent_and_replay_is_idempotent(self):
        context, daemon = self._started_real_git_view_daemon()
        repository = context.repository
        daemon.git.create_local_branch(repository, "runner-work", "main")
        parent_tip = self._commit_file(repository, "main", "parent.txt", "parent\n", "Parent")
        attempt_tip = self._commit_file(
            repository,
            "runner-work",
            "attempt.txt",
            "attempt\n",
            "Attempt",
        )
        daemon.view_init_readonly("source", attempt_tip)
        daemon.view_init_overlay("source", "runner-work")
        source_before = daemon._view_status(daemon.store.get("source"))
        self.assertFalse(source_before["desired_started"])
        self.assertFalse(source_before["dirty"])
        source_records = tuple(
            path.read_bytes()
            for path in (
                self.root / "views/source/config/view.json",
                self.root / "views/source/state/state.json",
            )
        )
        # Dirtiness on a different checked-out branch is unrelated to advancing the parent ref.
        unrelated = repository / "unrelated-untracked.txt"
        unrelated.write_text("operator data\n", encoding="utf-8")

        merged = self._daemon_request(
            daemon,
            "branch-merge",
            attempt_branch="runner-work",
            parent_branch="main",
            ensure_attempt_tip=attempt_tip,
            message="Merge runner-work",
            checked_out_destination="detach_if_clean",
        )
        self.assertTrue(merged["ok"])
        self.assertFalse(merged["idempotent"])
        self.assertEqual(merged["parent_tip_before"], parent_tip)
        merge_commit = merged["parent_tip_after"]
        self.assertEqual(daemon.git.resolve_local_branch(repository, "main"), merge_commit)
        self.assertEqual(daemon.git.resolve_local_branch(repository, "runner-work"), attempt_tip)
        self.assertEqual(
            subprocess.run(
                ("git", "-C", str(repository), "rev-list", "--parents", "-n", "1", merge_commit),
                check=True,
                stdout=subprocess.PIPE,
            )
            .stdout.decode("ascii")
            .split(),
            [merge_commit, parent_tip, attempt_tip],
        )
        self.assertEqual(daemon._view_status(daemon.store.get("source")), source_before)
        self.assertEqual(
            tuple(
                path.read_bytes()
                for path in (
                    self.root / "views/source/config/view.json",
                    self.root / "views/source/state/state.json",
                )
            ),
            source_records,
        )
        self.assertEqual(daemon.git.checkout_position(repository), (attempt_tip, "runner-work"))
        self.assertNotEqual(daemon.git.checkout_status(repository), b"")
        self.assertEqual(unrelated.read_text(encoding="utf-8"), "operator data\n")
        unrelated.unlink()
        self.assertEqual(daemon.git.checkout_status(repository), b"")

        daemon.view_start("source")
        subprocess.run(
            ("git", "-C", str(repository), "checkout", "--force", "main"),
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        replay = self._daemon_request(
            daemon,
            "branch-merge",
            attempt_branch="runner-work",
            parent_branch="main",
            ensure_attempt_tip=attempt_tip,
            message="Merge runner-work",
            checked_out_destination="detach_if_clean",
        )
        self.assertTrue(replay["ok"])
        self.assertTrue(replay["idempotent"])
        self.assertEqual(replay["parent_tip_before"], merge_commit)
        self.assertEqual(replay["parent_tip_after"], merge_commit)
        self.assertEqual(daemon.git.checkout_position(repository), (merge_commit, "main"))

    def test_branch_merge_default_refuses_a_checked_out_parent(self):
        context, daemon = self._started_real_git_view_daemon()
        repository = context.repository
        parent_tip = daemon.git.resolve_local_branch(repository, "main")
        daemon.git.create_local_branch(repository, "runner-refused", "main")
        attempt_tip = self._commit_file(
            repository,
            "runner-refused",
            "attempt.txt",
            "attempt\n",
            "Attempt",
        )
        subprocess.run(
            ("git", "-C", str(repository), "checkout", "--force", "main"),
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        refused = self._daemon_request(
            daemon,
            "branch-merge",
            attempt_branch="runner-refused",
            parent_branch="main",
            ensure_attempt_tip=attempt_tip,
            message="Merge runner-refused",
            checked_out_destination="refuse",
        )

        self.assertFalse(refused["ok"])
        self.assertEqual(refused["error"]["code"], "BRANCH_CHECKED_OUT")
        self.assertEqual(daemon.git.resolve_local_branch(repository, "main"), parent_tip)
        self.assertEqual(daemon.git.checkout_position(repository), (parent_tip, "main"))

    def test_branch_merge_detaches_clean_parent_without_rewriting_checkout(self):
        context, daemon = self._started_real_git_view_daemon()
        repository = context.repository
        parent_tip = self._commit_file(
            repository,
            "main",
            "parent.txt",
            "parent\n",
            "Parent",
        )
        daemon.git.create_local_branch(repository, "runner-detach", "main")
        attempt_tip = self._commit_file(
            repository,
            "runner-detach",
            "attempt.txt",
            "attempt\n",
            "Attempt",
        )
        subprocess.run(
            ("git", "-C", str(repository), "checkout", "--force", "main"),
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        index_tree_before = daemon.git.index_tree(repository)
        parent_contents_before = (repository / "parent.txt").read_bytes()
        self.assertFalse((repository / "attempt.txt").exists())

        merged = self._daemon_request(
            daemon,
            "branch-merge",
            attempt_branch="runner-detach",
            parent_branch="main",
            ensure_attempt_tip=attempt_tip,
            message="Merge runner-detach",
            checked_out_destination="detach_if_clean",
        )

        self.assertTrue(merged["ok"])
        self.assertFalse(merged["idempotent"])
        self.assertEqual(merged["parent_tip_before"], parent_tip)
        self.assertEqual(
            daemon.git.resolve_local_branch(repository, "main"),
            merged["parent_tip_after"],
        )
        self.assertEqual(daemon.git.checkout_position(repository), (parent_tip, None))
        self.assertEqual(daemon.git.index_tree(repository), index_tree_before)
        self.assertEqual((repository / "parent.txt").read_bytes(), parent_contents_before)
        self.assertFalse((repository / "attempt.txt").exists())
        self.assertEqual(daemon.git.checkout_status(repository), b"")
        self.assertIsNone(RootContext.load(self.root).repository_facts.branch)

    def test_branch_merge_dirty_parent_refuses_without_detaching_or_moving_refs(self):
        context, daemon = self._started_real_git_view_daemon()
        repository = context.repository
        parent_tip = daemon.git.resolve_local_branch(repository, "main")
        daemon.git.create_local_branch(repository, "runner-dirty", "main")
        attempt_tip = self._commit_file(
            repository,
            "runner-dirty",
            "attempt.txt",
            "attempt\n",
            "Attempt",
        )
        subprocess.run(
            ("git", "-C", str(repository), "checkout", "--force", "main"),
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        (repository / "untracked.txt").write_text("operator data\n", encoding="utf-8")

        refused = self._daemon_request(
            daemon,
            "branch-merge",
            attempt_branch="runner-dirty",
            parent_branch="main",
            ensure_attempt_tip=attempt_tip,
            message="Merge runner-dirty",
            checked_out_destination="detach_if_clean",
        )

        self.assertFalse(refused["ok"])
        self.assertEqual(refused["error"]["code"], "DIRTY_REPOSITORY")
        self.assertEqual(daemon.git.resolve_local_branch(repository, "main"), parent_tip)
        self.assertEqual(daemon.git.resolve_local_branch(repository, "runner-dirty"), attempt_tip)
        self.assertEqual(daemon.git.checkout_position(repository), (parent_tip, "main"))
        self.assertEqual((repository / "untracked.txt").read_text(encoding="utf-8"), "operator data\n")

    def test_branch_merge_preserves_typed_branch_and_tip_failures(self):
        context, daemon = self._started_real_git_view_daemon()
        repository = context.repository
        parent_tip = daemon.git.resolve_local_branch(repository, "main")

        missing_attempt = self._daemon_request(
            daemon,
            "branch-merge",
            attempt_branch="missing-attempt",
            parent_branch="main",
            ensure_attempt_tip="1" * 40,
            message="Missing attempt",
            checked_out_destination="detach_if_clean",
        )
        self.assertEqual(missing_attempt["error"]["code"], "ATTEMPT_BRANCH_NOT_FOUND")
        self.assertEqual(missing_attempt["error"]["data"], {"branch": "missing-attempt"})

        daemon.git.create_local_branch(repository, "runner-typed", "main")
        attempt_tip = self._commit_file(
            repository,
            "runner-typed",
            "attempt.txt",
            "attempt\n",
            "Attempt",
        )
        missing_parent = self._daemon_request(
            daemon,
            "branch-merge",
            attempt_branch="runner-typed",
            parent_branch="missing-parent",
            ensure_attempt_tip=attempt_tip,
            message="Missing parent",
            checked_out_destination="detach_if_clean",
        )
        self.assertEqual(missing_parent["error"]["code"], "PARENT_BRANCH_NOT_FOUND")
        self.assertEqual(missing_parent["error"]["data"], {"branch": "missing-parent"})

        mismatch = self._daemon_request(
            daemon,
            "branch-merge",
            attempt_branch="runner-typed",
            parent_branch="main",
            ensure_attempt_tip=parent_tip,
            message="Mismatched attempt",
            checked_out_destination="detach_if_clean",
        )
        self.assertEqual(mismatch["error"]["code"], "ATTEMPT_TIP_MISMATCH")
        self.assertEqual(
            mismatch["error"]["data"],
            {
                "attempt_branch": "runner-typed",
                "expected_attempt_tip": parent_tip,
                "actual_attempt_tip": attempt_tip,
            },
        )

    def test_branch_merge_checkout_tip_mismatch_refuses_before_detachment(self):
        context, daemon = self._started_real_git_view_daemon()
        repository = context.repository
        parent_tip = daemon.git.resolve_local_branch(repository, "main")
        daemon.git.create_local_branch(repository, "runner-mismatch", "main")
        attempt_tip = self._commit_file(
            repository,
            "runner-mismatch",
            "attempt.txt",
            "attempt\n",
            "Attempt",
        )
        subprocess.run(
            ("git", "-C", str(repository), "checkout", "--force", "main"),
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        with mock.patch.object(
            daemon.git,
            "checkout_position",
            return_value=(attempt_tip, "main"),
        ):
            refused = self._daemon_request(
                daemon,
                "branch-merge",
                attempt_branch="runner-mismatch",
                parent_branch="main",
                ensure_attempt_tip=attempt_tip,
                message="Merge runner-mismatch",
                checked_out_destination="detach_if_clean",
            )

        self.assertFalse(refused["ok"])
        self.assertEqual(refused["error"]["code"], "PARENT_TIP_MOVED")
        self.assertEqual(
            refused["error"]["data"],
            {
                "parent_branch": "main",
                "expected_parent_tip": parent_tip,
                "actual_parent_tip": attempt_tip,
            },
        )
        self.assertEqual(daemon.git.resolve_local_branch(repository, "main"), parent_tip)
        self.assertEqual(daemon.git.checkout_position(repository), (parent_tip, "main"))

    def test_branch_merge_cas_failure_retains_detached_checkout(self):
        context, daemon = self._started_real_git_view_daemon()
        repository = context.repository
        parent_tip = daemon.git.resolve_local_branch(repository, "main")
        daemon.git.create_local_branch(repository, "runner-cas", "main")
        daemon.git.create_local_branch(repository, "concurrent-parent", "main")
        attempt_tip = self._commit_file(
            repository,
            "runner-cas",
            "attempt.txt",
            "attempt\n",
            "Attempt",
        )
        concurrent_tip = self._commit_file(
            repository,
            "concurrent-parent",
            "concurrent.txt",
            "concurrent\n",
            "Concurrent parent",
        )
        subprocess.run(
            ("git", "-C", str(repository), "checkout", "--force", "main"),
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        original_repo = daemon.git._repo
        moved = False

        def move_before_publication(repo, arguments, **keywords):
            nonlocal moved
            if (
                not moved
                and tuple(arguments[:2]) == ("update-ref", "refs/heads/main")
            ):
                moved = True
                original_repo(
                    repo,
                    ("update-ref", "refs/heads/main", concurrent_tip, parent_tip),
                )
            return original_repo(repo, arguments, **keywords)

        with mock.patch.object(daemon.git, "_repo", side_effect=move_before_publication):
            refused = self._daemon_request(
                daemon,
                "branch-merge",
                attempt_branch="runner-cas",
                parent_branch="main",
                ensure_attempt_tip=attempt_tip,
                message="Merge runner-cas",
                checked_out_destination="detach_if_clean",
            )

        self.assertTrue(moved)
        self.assertFalse(refused["ok"])
        self.assertEqual(refused["error"]["code"], "PARENT_TIP_MOVED")
        self.assertEqual(
            refused["error"]["data"],
            {
                "parent_branch": "main",
                "expected_parent_tip": parent_tip,
                "actual_parent_tip": concurrent_tip,
            },
        )
        self.assertEqual(daemon.git.resolve_local_branch(repository, "main"), concurrent_tip)
        self.assertEqual(daemon.git.resolve_local_branch(repository, "runner-cas"), attempt_tip)
        self.assertEqual(daemon.git.checkout_position(repository), (parent_tip, None))
        self.assertEqual(daemon.git.checkout_status(repository), b"")

    def test_branch_merge_generic_publication_failure_retains_detached_checkout(self):
        context, daemon = self._started_real_git_view_daemon()
        repository = context.repository
        parent_tip = daemon.git.resolve_local_branch(repository, "main")
        daemon.git.create_local_branch(repository, "runner-publish-failure", "main")
        attempt_tip = self._commit_file(
            repository,
            "runner-publish-failure",
            "attempt.txt",
            "attempt\n",
            "Attempt",
        )
        subprocess.run(
            ("git", "-C", str(repository), "checkout", "--force", "main"),
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        original_repo = daemon.git._repo
        failed = False

        def fail_publication(repo, arguments, **keywords):
            nonlocal failed
            if (
                not failed
                and tuple(arguments[:2]) == ("update-ref", "refs/heads/main")
            ):
                failed = True
                return subprocess.CompletedProcess(
                    args=("git", *arguments),
                    returncode=1,
                    stdout=b"",
                    stderr=b"injected update-ref failure",
                )
            return original_repo(repo, arguments, **keywords)

        with mock.patch.object(daemon.git, "_repo", side_effect=fail_publication):
            refused = self._daemon_request(
                daemon,
                "branch-merge",
                attempt_branch="runner-publish-failure",
                parent_branch="main",
                ensure_attempt_tip=attempt_tip,
                message="Merge runner-publish-failure",
                checked_out_destination="detach_if_clean",
            )

        self.assertTrue(failed)
        self.assertFalse(refused["ok"])
        self.assertEqual(refused["error"]["code"], "GIT_FAILED")
        self.assertEqual(daemon.git.resolve_local_branch(repository, "main"), parent_tip)
        self.assertEqual(daemon.git.checkout_position(repository), (parent_tip, None))
        self.assertEqual(daemon.git.checkout_status(repository), b"")

    def test_branch_merge_detach_policy_still_refuses_managed_repository_transition(self):
        context, daemon = self._started_real_git_view_daemon()
        repository = context.repository
        parent_tip = daemon.git.resolve_local_branch(repository, "main")
        daemon.git.create_local_branch(repository, "runner-transition", "main")
        attempt_tip = self._commit_file(
            repository,
            "runner-transition",
            "attempt.txt",
            "attempt\n",
            "Attempt",
        )
        daemon.store.repository_operation = {"operation": "sync"}

        with self.assertRaises(GitViewspaceError) as raised:
            daemon.branch_merge(
                "runner-transition",
                "main",
                attempt_tip,
                "Merge runner-transition",
                "detach_if_clean",
            )

        self.assertEqual(raised.exception.code, "GIT_OPERATION_IN_PROGRESS")
        self.assertEqual(daemon.git.resolve_local_branch(repository, "main"), parent_tip)
        self.assertEqual(daemon.git.resolve_local_branch(repository, "runner-transition"), attempt_tip)

    def test_branch_merge_conflict_reports_bounded_repository_paths_without_moving_refs(self):
        context, daemon = self._started_real_git_view_daemon()
        repository = context.repository
        (repository / "shared.txt").write_text("base\n", encoding="utf-8")
        subprocess.run(("git", "-C", str(repository), "add", "shared.txt"), check=True)
        subprocess.run(
            (
                "git",
                "-c",
                "user.name=Managed User",
                "-c",
                "user.email=managed@example.invalid",
                "-C",
                str(repository),
                "commit",
                "-m",
                "Shared base",
            ),
            check=True,
            stdout=subprocess.DEVNULL,
        )
        daemon.git.create_local_branch(repository, "runner-conflict", "main")
        parent_tip = self._commit_file(repository, "main", "shared.txt", "parent\n", "Parent")
        attempt_tip = self._commit_file(
            repository,
            "runner-conflict",
            "shared.txt",
            "attempt\n",
            "Attempt",
        )
        subprocess.run(
            ("git", "-C", str(repository), "checkout", "--force", "main"),
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        conflicted = self._daemon_request(
            daemon,
            "branch-merge",
            attempt_branch="runner-conflict",
            parent_branch="main",
            ensure_attempt_tip=attempt_tip,
            message="Merge runner-conflict",
            checked_out_destination="detach_if_clean",
        )
        self.assertFalse(conflicted["ok"])
        self.assertEqual(conflicted["error"]["code"], "BRANCH_MERGE_CONFLICT")
        self.assertEqual(
            conflicted["error"]["data"],
            {
                "attempt_branch": "runner-conflict",
                "attempt_tip": attempt_tip,
                "parent_branch": "main",
                "parent_tip": parent_tip,
                "conflict_path_count": 1,
                "conflict_paths_rr": ["shared.txt"],
                "conflict_paths_truncated": False,
            },
        )
        self.assertEqual(daemon.git.resolve_local_branch(repository, "main"), parent_tip)
        self.assertEqual(daemon.git.resolve_local_branch(repository, "runner-conflict"), attempt_tip)
        self.assertEqual(daemon.git.checkout_position(repository), (parent_tip, "main"))
        self.assertEqual(daemon.git.checkout_status(repository), b"")

    def test_branch_merge_refuses_a_parent_claimed_by_a_writable_view(self):
        context, daemon = self._started_real_git_view_daemon()
        repository = context.repository
        daemon.git.create_local_branch(repository, "runner-claimed", "main")
        attempt_tip = self._commit_file(
            repository,
            "runner-claimed",
            "attempt.txt",
            "attempt\n",
            "Attempt",
        )
        parent_tip = daemon.git.resolve_local_branch(repository, "main")
        daemon.view_init_readonly("parent-owner", parent_tip)
        daemon.view_init_overlay("parent-owner", "main")

        refused = self._daemon_request(
            daemon,
            "branch-merge",
            attempt_branch="runner-claimed",
            parent_branch="main",
            ensure_attempt_tip=attempt_tip,
            message="Merge runner-claimed",
            checked_out_destination="detach_if_clean",
        )
        self.assertFalse(refused["ok"])
        self.assertEqual(refused["error"]["code"], "BRANCH_CLAIMED")
        self.assertEqual(refused["error"]["data"]["view_name"], "parent-owner")
        self.assertEqual(daemon.git.resolve_local_branch(repository, "main"), parent_tip)
        self.assertEqual(daemon.git.resolve_local_branch(repository, "runner-claimed"), attempt_tip)

    def test_cli_dispatches_repository_branch_merge(self):
        payload = {
            "ok": True,
            "command": "branch merge",
            "root": str(self.root),
            "attempt_branch": "runner-work",
            "attempt_tip": "1" * 40,
            "parent_branch": "main",
            "parent_tip_before": "2" * 40,
            "parent_tip_after": "3" * 40,
            "idempotent": False,
        }
        output = io.StringIO()
        with (
            mock.patch("git_viewspace.cli.branch_merge", return_value=payload) as operation,
            contextlib.redirect_stdout(output),
        ):
            result = cli_run(
                (
                    "--root",
                    str(self.root),
                    "--json",
                    "branch",
                    "merge",
                    "runner-work",
                    "--into",
                    "main",
                    "--ensure-attempt-tip",
                    "1" * 40,
                    "--message",
                    "Merge runner-work",
                )
            )
        self.assertEqual(result, 0)
        operation.assert_called_once_with(
            str(self.root),
            "runner-work",
            "main",
            "1" * 40,
            "Merge runner-work",
            "refuse",
        )
        self.assertEqual(json.loads(output.getvalue()), payload)
        parsed = cli_parser().parse_args(
            (
                "--root",
                str(self.root),
                "branch",
                "merge",
                "runner-work",
                "--into",
                "main",
                "--ensure-attempt-tip",
                "1" * 40,
                "--message",
                "Merge runner-work",
                "--checked-out-destination",
                "detach-if-clean",
            )
        )
        self.assertEqual(parsed.checked_out_destination, "detach-if-clean")

    def test_compound_init_and_advance_run_one_checked_state_machine_request(self):
        context, daemon = self._started_real_git_view_daemon()
        initialized = daemon.view_init_and_start(
            "work",
            "overlay",
            from_branch="main",
            new_branch="runner-work",
        )
        self.assertEqual(
            [step["name"] for step in initialized["steps"]],
            ["init-readonly", "init-overlay", "start"],
        )
        self.assertEqual(initialized["view"]["actual"], "overlay")
        self.assertEqual(
            initialized["branch_creation"],
            {
                "parent_branch": "main",
                "parent_commit": context.repository_facts.head,
                "new_branch": "runner-work",
                "new_branch_commit": context.repository_facts.head,
            },
        )
        self.assertEqual(
            daemon.git.resolve_local_branch(context.repository, "runner-work"),
            context.repository_facts.head,
        )
        with self.assertRaises(GitViewspaceError) as raised:
            daemon.view_init_and_start(
                "work",
                "overlay",
                from_branch="main",
                new_branch="another-runner",
            )
        self.assertEqual(raised.exception.code, "VIEW_EXISTS")
        with self.assertRaises(GitViewspaceError) as raised:
            daemon.git.resolve_local_branch(context.repository, "another-runner")
        self.assertEqual(raised.exception.code, "BRANCH_NOT_FOUND")
        with self.assertRaises(GitViewspaceError) as raised:
            daemon.view_init_and_start(
                "other",
                "overlay",
                from_branch="main",
                new_branch="main",
            )
        self.assertEqual(raised.exception.code, "BRANCH_EXISTS")
        self.assertNotIn("other", daemon.store.views)
        view = daemon.store.get("work")
        (view.upper_path / "created.txt").write_text("created\n", encoding="utf-8")
        helper = _FakeSync(
            {
                **_FakeSync().report,
                "git_pathspecs": ["created.txt"],
                "whiteouts": [],
                "counts": {
                    "directories": 0,
                    "regulars": 1,
                    "symlinks": 0,
                    "whiteouts": 0,
                    "opaque": 0,
                    "origin": 0,
                    "impure": 0,
                },
            }
        )

        def replay(_upper, checkout):
            (Path(checkout) / "created.txt").write_text("created\n", encoding="utf-8")
            return {**helper.report, "mode": "direct"}

        helper.replay = replay
        daemon.sync = helper
        advanced = daemon.view_advance("work", "Compound transition")
        self.assertEqual(
            [step["name"] for step in advanced["steps"]],
            ["stop", "sync", "stage", "commit", "clean", "switch-next", "start"],
        )
        self.assertEqual(advanced["view"]["actual"], "overlay")
        self.assertNotEqual(advanced["view"]["commit"], context.repository_facts.head)
        self.assertEqual(daemon.restart_count, 0)
        self.assertEqual(daemon.viewfs.generation, 1)
        self.assertIsNone(daemon.store.repository_operation)

    def test_noop_transition_records_next_without_empty_commit(self):
        context, daemon = self._started_real_git_view_daemon()
        daemon.view_init_readonly("work", "HEAD")
        daemon.view_init_overlay("work", "main")
        helper = _FakeSync(
            {
                **_FakeSync().report,
                "git_pathspecs": [],
                "whiteouts": [],
                "counts": {
                    "directories": 0,
                    "regulars": 0,
                    "symlinks": 0,
                    "whiteouts": 0,
                    "opaque": 0,
                    "origin": 0,
                    "impure": 0,
                },
            }
        )
        daemon.sync = helper
        daemon.view_sync("work")
        daemon.view_stage("work")
        committed = daemon.view_commit("work", "No-op transition")
        self.assertFalse(committed["next"]["changed"])
        self.assertEqual(committed["next"]["commit"], context.repository_facts.head)
        self.assertEqual(daemon.git.resolve_local_branch(context.repository, "main"), context.repository_facts.head)
        daemon.view_clean("work")
        switched = daemon.view_switch("work", use_next=True)
        self.assertEqual(switched["view"]["commit"], context.repository_facts.head)
        self.assertIsNone(switched["view"]["next"])

    def test_sync_uses_one_authoritative_replay_report(self):
        _context, daemon = self._started_view_daemon()
        daemon.view_init_readonly("work", "HEAD")
        daemon.view_init_overlay("work", "main")
        helper = _FakeSync()
        replay = {**helper.report, "mode": "direct", "upper_fingerprint": "b" * 64}
        helper.replay = mock.Mock(return_value=replay)
        daemon.sync = helper
        daemon.git.prepare_sync_checkout = mock.Mock(return_value="b" * 40)
        result = daemon.view_sync("work")
        self.assertEqual(result["sync"]["upper_fingerprint"], "b" * 64)
        helper.replay.assert_called_once()

    def test_unsupported_upper_fails_without_publishing_sync_state(self):
        _context, daemon = self._started_view_daemon()
        daemon.view_init_readonly("work", "HEAD")
        daemon.view_init_overlay("work", "main")
        helper = _FakeSync()
        helper.replay = mock.Mock(
            side_effect=GitViewspaceError(
                "UNSUPPORTED_OVERLAY_ARTIFACT",
                "unsupported test artifact",
            )
        )
        daemon.sync = helper
        daemon.git.prepare_sync_checkout = mock.Mock()
        with self.assertRaises(GitViewspaceError) as caught:
            daemon.view_sync("work")
        self.assertEqual(caught.exception.code, "UNSUPPORTED_OVERLAY_ARTIFACT")
        daemon.git.prepare_sync_checkout.assert_called_once()

    def test_invalid_redirect_source_is_rejected_by_authoritative_replay(self):
        _context, daemon = self._started_view_daemon()
        daemon.view_init_readonly("work", "HEAD")
        daemon.view_init_overlay("work", "main")
        helper = _FakeSync()
        helper.replay = mock.Mock(
            side_effect=GitViewspaceError(
                "UNSUPPORTED_OVERLAY_ARTIFACT",
                "redirect source is absent",
            )
        )
        daemon.sync = helper
        daemon.git.prepare_sync_checkout = mock.Mock()
        with self.assertRaises(GitViewspaceError) as caught:
            daemon.view_sync("work")
        self.assertEqual(caught.exception.code, "UNSUPPORTED_OVERLAY_ARTIFACT")
        daemon.git.prepare_sync_checkout.assert_called_once()


if __name__ == "__main__":
    unittest.main()
