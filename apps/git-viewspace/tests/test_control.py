"""Connection isolation and bounded, serialized control service qualification."""

import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from git_viewspace.control import Control
from git_viewspace.daemon import Daemon
from git_viewspace.errors import GitViewspaceError
from git_viewspace.protocol import (
    MAX_MESSAGE_BYTES,
    call,
    receive_message,
    request,
    send_message,
    validate_request,
)


class TransportTests(unittest.TestCase):
    def daemon(self):
        daemon = object.__new__(Daemon)
        daemon.context = SimpleNamespace(root="/fixture", root_uuid="fixture")
        daemon._dispatch = mock.Mock(return_value={"ok": True})
        return daemon

    def test_idle_and_fragmented_deadlines_then_valid_request(self):
        daemon = self.daemon()
        for fragment in (False, True):
            server, client = socket.socketpair()
            with server, client, mock.patch("git_viewspace.daemon.REQUEST_READ_TIMEOUT_SECONDS", 0.08):
                thread = threading.Thread(target=daemon._serve_connection, args=(server,))
                thread.start()
                if fragment:
                    for _ in range(6):
                        client.send(b" ")
                        time.sleep(0.02)
                thread.join(1)
                self.assertFalse(thread.is_alive())
        daemon._dispatch.assert_not_called()
        server, client = socket.socketpair()
        with server, client:
            send_message(client, request("status"))
            daemon._serve_connection(server)
            self.assertTrue(receive_message(client, "response")["ok"])
        daemon._dispatch.assert_called_once()

    def test_invalid_closed_oversized_and_failed_reply_are_local(self):
        daemon = self.daemon()
        for payload in (b"", b"not-json\n", b" " * (MAX_MESSAGE_BYTES + 1)):
            stream = mock.Mock()
            pieces = [payload, b""]
            stream.recv.side_effect = lambda _size, pieces=pieces: pieces.pop(0)
            daemon._serve_connection(stream)
        daemon._dispatch.assert_not_called()
        stream = mock.Mock()
        # Use the producer, so the valid case follows protocol upgrades.
        from git_viewspace.storage import canonical_json_bytes

        stream.recv.return_value = canonical_json_bytes(request("status"))
        stream.sendall.side_effect = BrokenPipeError()
        daemon._serve_connection(stream)
        daemon._dispatch.assert_called_once()

    def test_execution_io_error_is_not_hidden_as_client_disconnect(self):
        daemon = self.daemon()
        daemon._dispatch.side_effect = OSError("filesystem failed")
        server, client = socket.socketpair()
        with server, client:
            send_message(client, request("status"))
            with self.assertRaisesRegex(OSError, "filesystem failed"):
                daemon._serve_connection(server)


class FixtureDaemon:
    _error_response = staticmethod(Daemon._error_response)
    _timed_step = Daemon._timed_step

    def __init__(self, root):
        self.context = SimpleNamespace(root=root, root_uuid="fixture", socket_path=root / "s")
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(str(self.context.socket_path))
        self.server.listen(128)
        self.signal_stop = self.stopping = False
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = []
        self.threads = set()
        self.observations = 0
        self.observation_error = False
        self.stops = 0

    def _validate_durable_state(self, *_args):
        self.threads.add(threading.get_ident())

    def status(self):
        self.threads.add(threading.get_ident())
        self.observations += 1
        if self.observation_error:
            raise GitViewspaceError("STALE_DURABLE_STATE", "fixture observation failed")
        return {
            "ok": True,
            "command": "status",
            "healthy": True,
            "views": [{"name": "one", "view_id": "1" * 32, "commit": str(len(self.calls))}],
        }

    def supervise(self):
        self.threads.add(threading.get_ident())

    def _dispatch(self, value):
        self.threads.add(threading.get_ident())
        if value["command"] == "stop":
            self.stops += 1
            self.stopping = True
            self.server.close()
            return {"ok": True, "command": "stop"}
        name = value["message"]
        if name == "fatal":
            raise RuntimeError("fixture internal error")
        if name == "error":
            return self._error_response(value["command"], GitViewspaceError("REFUSED", "fixture"))

        def operation():
            if name == "blocked":
                self.entered.set()
                if not self.release.wait(5):
                    raise RuntimeError("fixture barrier expired")
            self.calls.append(name)
            return {"ok": True}

        result, _ = self._timed_step("fixture-commit", operation)
        return result


class ControlTests(unittest.TestCase):
    def start(self, daemon=None):
        temporary = tempfile.TemporaryDirectory(prefix="ct-")
        self.addCleanup(temporary.cleanup)
        self.daemon = daemon or FixtureDaemon(Path(temporary.name))
        self.control = Control(self.daemon)
        self.errors = []

        def serve():
            try:
                self.control.run()
            except Exception as error:
                self.errors.append(error)

        self.thread = threading.Thread(target=serve)
        self.thread.start()
        self.addCleanup(self.finish)
        self.wait(lambda: self.control.snapshot is not None)

    def finish(self):
        if hasattr(self.daemon, "release"):
            self.daemon.release.set()
        self.daemon.signal_stop = True
        self.thread.join(5)
        self.assertFalse(self.thread.is_alive())
        if self.daemon.server is not None:
            self.daemon.server.close()

    def wait(self, predicate):
        end = time.monotonic() + 3
        while not predicate():
            if time.monotonic() > end:
                self.fail("condition not reached")
            time.sleep(0.005)

    def connect(self, name="blocked", **extra):
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(3)
        client.connect(str(self.daemon.context.socket_path))
        self.addCleanup(client.close)
        send_message(client, request("view-advance", view="one", message=name, **extra))
        return client

    def status(self, **extra):
        return call(self.daemon.context.socket_path, "status", timeout=1, **extra)

    def test_snapshot_remains_responsive_fifo_and_single_owner(self):
        self.start()
        first = self.connect()
        self.assertTrue(self.daemon.entered.wait(1))
        baseline = self.daemon.observations
        queued = self.connect("second")
        self.wait(lambda: self.status()["activity"]["queued"] == 1)
        for _ in range(10):
            state = self.status()
            self.assertEqual(state["views"][0]["commit"], "0")
            self.assertEqual(state["activity"]["running"]["phase"], "fixture-commit")
            self.assertEqual(state["observation"]["kind"], "last_consistent_snapshot")
        self.assertEqual(self.daemon.observations, baseline)
        self.daemon.release.set()
        self.assertTrue(receive_message(first, "reply")["ok"])
        result = receive_message(queued, "reply")
        self.assertTrue(result["ok"])
        self.assertGreater(result["timing"]["queue_seconds"], 0)
        self.assertEqual(self.daemon.calls, ["blocked", "second"])
        self.assertEqual(len(self.daemon.threads), 1)
        self.assertFalse(self.errors)

    def test_queue_capacity_expiry_and_disconnected_pending_request(self):
        with mock.patch("git_viewspace.control.MAX_QUEUED", 2):
            self.start()
            first = self.connect()
            self.assertTrue(self.daemon.entered.wait(1))
            expired = self.connect("expired", queue_timeout_seconds=0.15)
            abandoned = self.connect("abandoned")
            self.wait(lambda: self.status()["activity"]["queued"] == 2)
            refused = receive_message(self.connect("refused"), "reply")
            self.assertEqual(refused["error"]["code"], "DAEMON_BUSY")
            self.assertFalse(refused["error"]["data"]["executed"])
            refused = receive_message(expired, "reply")
            self.assertEqual(refused["error"]["code"], "REQUEST_EXPIRED")
            self.assertFalse(refused["error"]["data"]["executed"])
            abandoned.close()
            self.wait(lambda: self.status()["activity"]["queued"] == 0)
            self.daemon.release.set()
            self.assertTrue(receive_message(first, "reply")["ok"])
            self.assertEqual(self.daemon.calls, ["blocked"])

    def test_disconnect_after_dispatch_does_not_cancel_or_replay(self):
        self.start()
        first = self.connect()
        self.assertTrue(self.daemon.entered.wait(1))
        first.close()
        self.daemon.release.set()
        self.wait(lambda: self.status()["activity"]["running"] is None)
        self.assertEqual(self.daemon.calls, ["blocked"])

    def test_stop_and_signal_abandon_queue_but_wait_for_running_operation(self):
        for mode in ("stop", "signal", "disconnected-stop"):
            with self.subTest(mode=mode):
                self.start()
                first = self.connect()
                self.assertTrue(self.daemon.entered.wait(1))
                queued = self.connect("unstarted")
                self.wait(lambda: self.status()["activity"]["queued"] == 1)
                stop = None
                if mode == "signal":
                    self.daemon.signal_stop = True
                else:
                    stop = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    stop.settimeout(3)
                    self.addCleanup(stop.close)
                    stop.connect(str(self.daemon.context.socket_path))
                    send_message(stop, request("stop"))
                refusal = receive_message(queued, "reply")
                self.assertEqual(refusal["error"]["code"], "DAEMON_STOPPING")
                self.assertFalse(refusal["error"]["data"]["executed"])
                self.assertEqual(self.daemon.stops, 0)
                if mode == "disconnected-stop":
                    stop.close()
                    stop = None
                self.daemon.release.set()
                self.assertTrue(receive_message(first, "reply")["ok"])
                if stop:
                    self.assertTrue(receive_message(stop, "reply")["ok"])
                self.thread.join(3)
                self.assertFalse(self.thread.is_alive())
                self.assertEqual(self.daemon.calls, ["blocked"])
                self.assertEqual(self.daemon.stops, 1)

    def test_expected_errors_observation_errors_and_fatal_execution(self):
        self.start()
        failed = receive_message(self.connect("error"), "reply")
        self.assertEqual(failed["error"]["code"], "REFUSED")
        self.assertIn("timing", failed["error"]["data"])
        self.daemon.observation_error = True
        receive_message(self.connect("ok"), "reply")
        state = self.status()
        self.assertEqual(state["error"]["code"], "STALE_DURABLE_STATE")
        self.assertIn("activity", state["error"]["data"])
        failed = receive_message(self.connect("fatal"), "reply")
        self.assertEqual(failed["error"]["code"], "DAEMON_FAILED")
        self.thread.join(3)
        self.assertFalse(self.thread.is_alive())
        self.assertEqual(self.daemon.stops, 1)
        self.assertEqual(len(self.errors), 1)

    def test_peer_limit_idle_bad_input_and_identity_guards(self):
        with (
            mock.patch("git_viewspace.control.MAX_PEERS", 3),
            mock.patch("git_viewspace.control.IO_SECONDS", 0.1),
        ):
            self.start()
            idle = []
            for _ in range(3):
                peer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                peer.connect(str(self.daemon.context.socket_path))
                idle.append(peer)
                self.addCleanup(peer.close)
            self.wait(lambda: len(self.control.peers) == 3)
            fourth = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            fourth.settimeout(1)
            self.addCleanup(fourth.close)
            fourth.connect(str(self.daemon.context.socket_path))
            self.assertEqual(fourth.recv(1), b"")
            self.wait(lambda: not self.control.peers)
            self.assertTrue(self.status()["ok"])
            bad = self.connect("normal", queue_timeout_seconds=-1)
            self.assertEqual(receive_message(bad, "reply")["error"]["code"], "INVALID_REQUEST")
            huge = self.connect("normal", queue_timeout_seconds=10**400)
            self.assertEqual(receive_message(huge, "reply")["error"]["code"], "INVALID_REQUEST")
            self.assertTrue(self.status()["ok"])
            response = call(
                self.daemon.context.socket_path, "view-status", view="one", ensure_view_id="2" * 32
            )
            self.assertEqual(response["error"]["code"], "VIEW_ID_MISMATCH")
            response = call(self.daemon.context.socket_path, "view-status", view="missing")
            self.assertEqual(response["error"]["code"], "VIEW_NOT_FOUND")
            self.assertEqual(self.daemon.calls, [])

    def test_protocol_queue_budget_and_prior_version_rejection(self):
        for budget in (0, -1, True, None, float("inf"), float("nan"), 86401, 10**400):
            with self.subTest(budget=budget), self.assertRaises(GitViewspaceError):
                validate_request(request("stop", queue_timeout_seconds=budget))
        self.assertEqual(
            validate_request(request("stop", queue_timeout_seconds=0.5))["queue_timeout_seconds"],
            0.5,
        )
        with self.assertRaises(GitViewspaceError):
            validate_request({"protocol": 3, "command": "status"})
        with self.assertRaises(GitViewspaceError):
            validate_request(request("status", queue_timeout_seconds=1))

    def test_slow_supervision_observation_does_not_starve_mutations(self):
        self.start()
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        original = self.daemon.status
        observations = []

        def slow_observe():
            if observations:
                self.assertEqual(self.daemon.calls, ["after-supervision"])
            observations.append(True)
            entered.set()
            release.wait(3)
            return original()

        with mock.patch.object(self.daemon, "status", side_effect=slow_observe):
            self.control.last_supervised = 0
            self.assertTrue(entered.wait(1))
            queued = self.connect("after-supervision")
            # The completed supervision/observation interval exceeds its scheduling period.
            with mock.patch("git_viewspace.control.OBSERVE_SECONDS", 0.05):
                time.sleep(0.1)
                self.assertTrue(self.status()["ok"])
                release.set()
                self.assertTrue(receive_message(queued, "reply")["ok"])
        self.assertEqual(self.daemon.calls, ["after-supervision"])
        self.assertEqual(len(self.daemon.threads), 1)
        self.assertFalse(self.errors)

    def test_selector_trickling_oversize_and_stalled_reply_are_local(self):
        with mock.patch("git_viewspace.control.IO_SECONDS", 0.15):
            self.start()
            slow = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            slow.connect(str(self.daemon.context.socket_path))
            self.addCleanup(slow.close)
            self.wait(lambda: len(self.control.peers) == 1)
            peer = next(iter(self.control.peers))
            for _ in range(4):
                slow.send(b" ")
                self.assertTrue(self.status()["ok"])
                time.sleep(0.025)
            self.wait(lambda: peer not in self.control.peers)

            for payload, code in (
                (b"not-json\n", "PROTOCOL_ERROR"),
                (b" " * (MAX_MESSAGE_BYTES + 1), "PROTOCOL_ERROR"),
                (b'{"protocol":4,"command":"view-status","view":"\\ud800"}\n', "INVALID_REQUEST"),
            ):
                bad = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                bad.settimeout(2)
                bad.connect(str(self.daemon.context.socket_path))
                with bad:
                    bad.sendall(payload)
                    self.assertEqual(receive_message(bad, "reply")["error"]["code"], code)
                self.assertTrue(self.status()["ok"])

            stalled = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            stalled.connect(str(self.daemon.context.socket_path))
            self.addCleanup(stalled.close)
            self.wait(lambda: len(self.control.peers) == 1)
            peer = next(iter(self.control.peers))
            peer.socket.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
            original = self.control._status

            def big_status(value):
                response = original(value)
                if value["command"] == "view-status":
                    response["padding"] = "x" * (512 * 1024)
                return response

            with mock.patch.object(self.control, "_status", side_effect=big_status):
                send_message(stalled, request("view-status", view="one"))
                self.wait(lambda: peer.state == "writing" and bool(peer.output))
                self.assertTrue(self.status()["ok"])
                self.wait(lambda: peer not in self.control.peers)
            self.assertTrue(self.status()["ok"])
            self.assertFalse(self.errors)

    def test_public_snapshot_during_missing_view_record_and_recovery(self):
        from git_viewspace import service
        from git_viewspace.root import OnlineRoot
        from git_viewspace.storage import atomic_write

        from tests.test_stage2 import Stage2Test, _FakeMounts

        fixture = Stage2Test()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        context, daemon = fixture._started_fake_daemon()
        daemon.mounts = _FakeMounts(context)
        daemon.view_init_readonly("one", "HEAD")
        daemon._bind_socket()
        view = daemon.store.get("one")
        saved = view.config_path.read_bytes()
        entered, release = threading.Event(), threading.Event()
        daemon.release = release
        original = daemon._dispatch

        def dispatch(value):
            if value["command"] != "view-advance":
                return original(value)
            daemon._write_runtime(ready=False)
            view.config_path.unlink()
            entered.set()
            try:
                if not release.wait(3):
                    raise RuntimeError("test barrier expired")
            finally:
                atomic_write(view.config_path, saved)
                daemon._write_runtime(ready=True)
            return {"ok": True}

        daemon._dispatch = dispatch
        self.start(daemon)
        first = self.connect()
        self.assertTrue(entered.wait(1))
        with self.assertRaises(GitViewspaceError):
            OnlineRoot.load(context.root)
        with self.assertRaises(GitViewspaceError):
            OnlineRoot.load(context.root, view="one", allow_unready=True)
        state = service.view_status(context.root, "one", ensure_view_id=view.view_id)
        self.assertEqual(state["view"]["view_id"], view.view_id)
        self.assertEqual(state["observation"]["kind"], "last_consistent_snapshot")
        self.assertTrue(service.status(context.root)["ok"])
        release.set()
        self.assertTrue(receive_message(first, "reply")["ok"])

    def test_service_cli_preserve_refusal_and_observation_diagnostics(self):
        import contextlib
        import io
        import json

        from git_viewspace import service
        from git_viewspace.cli import run

        self.start()
        with mock.patch("git_viewspace.service.OnlineRoot.load", return_value=self.daemon.context):
            with mock.patch("git_viewspace.control.MAX_QUEUED", 0):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    code = run(
                        [
                            "--root",
                            str(self.daemon.context.root),
                            "--json",
                            "view",
                            "advance",
                            "one",
                            "--message",
                            "not executed",
                        ]
                    )
                self.assertNotEqual(code, 0)
                data = json.loads(output.getvalue())["error"]["data"]
                self.assertFalse(data["executed"])
                self.assertIn("timing", data)
            self.daemon.observation_error = True
            receive_message(self.connect("ok"), "reply")
            with self.assertRaises(GitViewspaceError) as caught:
                service.status(self.daemon.context.root)
            self.assertEqual(caught.exception.code, "STALE_DURABLE_STATE")
            self.assertIn("activity", caught.exception.data)
            self.assertIn("observation", caught.exception.data)

    def test_fallback_shutdown_holds_both_ownership_locks(self):
        import contextlib
        import io

        from git_viewspace.daemon import run

        held = set()

        @contextlib.contextmanager
        def lock(path):
            held.add(path)
            try:
                yield
            finally:
                held.remove(path)

        context = SimpleNamespace(root_lock="root", repository_lock="repository")
        daemon = mock.Mock(stopping=False)
        daemon.serve.side_effect = RuntimeError("fixture failure")

        def shutdown():
            self.assertEqual(held, {"root", "repository"})
            daemon.stopping = True

        daemon._shutdown.side_effect = shutdown
        with (
            mock.patch("git_viewspace.daemon.RootContext.load", return_value=context),
            mock.patch("git_viewspace.daemon.exclusive_lock", side_effect=lock),
            mock.patch("git_viewspace.daemon.signal.signal"),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(run("/fixture", daemon_factory=lambda _: daemon), 1)
        daemon._shutdown.assert_called_once()
        self.assertEqual(held, set())
