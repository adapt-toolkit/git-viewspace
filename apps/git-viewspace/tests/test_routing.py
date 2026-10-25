"""Routing contracts independent of privileged mount availability."""

import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from git_viewspace.errors import GitViewspaceError
from git_viewspace.protocol import request, validate_request
from git_viewspace.routing import PREFIX, _table


class RoutingTest(unittest.TestCase):
    def test_descriptor_parser_native(self):
        import shutil
        import subprocess
        compiler = shutil.which("c++")
        if not compiler:
            self.skipTest("C++17 compiler unavailable")
        source = Path(__file__).resolve().parents[2] / "git-viewspace-router/test_descriptor.cc"
        with tempfile.TemporaryDirectory() as tmp:
            binary = Path(tmp) / "descriptor-test"
            subprocess.run([compiler, "-std=c++17", "-Wall", "-Wextra", str(source), "-o", str(binary)], check=True, capture_output=True)
            subprocess.run([str(binary)], check=True, capture_output=True)

    def test_subtree_validation_and_module_context(self):
        from git_viewspace.routing import validate_subtree, selected_source
        for value in ("/absolute", "../escape", "a//b", ".", "a/.git"):
            with self.assertRaises(GitViewspaceError):
                validate_subtree(value)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "routing").mkdir()
            (root / "session").mkdir()
            (root / "alias").symlink_to(root / "session", target_is_directory=True)
            view = SimpleNamespace(path=root, commit="a" * 40, routing={"subtree": "session"})
            self.assertEqual(selected_source(view, root), root / "session")
            mounts = SimpleNamespace(context=SimpleNamespace(root=root, repository=root))
            with patch("git_viewspace.submodules.entries", return_value={"session/tools": "x", "unrelated": "y"}):
                self.assertEqual(_table(mounts, view, root).read_bytes(), str(root).encode() + b"\0/tools\0")
            view.routing = {"subtree": "alias"}
            with self.assertRaises(GitViewspaceError):
                selected_source(view, root)

    def test_legacy_identity_fault_survives_upgrade(self):
        import hashlib
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            directory = root / "routing"
            directory.mkdir()
            legacy = {"/credential": {"target": "/external", "device": 11, "inode": 22, "fault": "old.fault"}}
            (directory / "bindings.json").write_text(json.dumps(legacy))
            (directory / "bindings.json").chmod(0o600)
            (directory / "old.fault").write_text("old error")
            view = SimpleNamespace(path=root, commit="a" * 40, routing={})
            mounts = SimpleNamespace(context=SimpleNamespace(root=root, repository=root, uid=os.getuid(), gid=os.getgid()))
            with patch("git_viewspace.submodules.entries", return_value={}):
                _table(mounts, view, root / "unused")
            key = hashlib.sha256(b"/external\n").hexdigest()
            self.assertEqual((directory / (key + ".identity")).read_text(), "11 22\n")
            self.assertEqual((directory / (key + ".fault")).read_text(), "old error")
            self.assertTrue((directory / "bindings.migrated.json").exists())

    def test_protocol_requires_explicit_valid_configuration(self):
        for action in ("disable", "rebind"):
            validate_request(
                request("view-routing", view="example", action=action, executable=None)
            )
        validate_request(
            request(
                "view-routing",
                view="example",
                action="enable",
                executable="/opt/router",
            )
        )
        for action, executable in [
            ("enable", None),
            ("enable", "relative"),
            ("disable", "/opt/router"),
            ("unknown", None),
        ]:
            with self.assertRaises(GitViewspaceError):
                validate_request(
                    request(
                        "view-routing",
                        view="example",
                        action=action,
                        executable=executable,
                    )
                )

    def test_context_creation_never_walks_or_opens_view(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "routing").mkdir()
            view = SimpleNamespace(path=root, commit="a" * 40, routing={})
            context = SimpleNamespace(root=root, repository=root)
            with (
                patch("git_viewspace.submodules.entries", return_value={"code": "unused"}),
                patch("os.walk", side_effect=AssertionError("whole-view scan")),
            ):
                table = _table(SimpleNamespace(context=context), view, root / "nonexistent-view")
                self.assertEqual(table.read_bytes(), str(root).encode() + b"\0/code\0")
                self.assertFalse((root / "routing/bindings.json").exists())

    def test_reconcile_cleans_private_only_router_residue(self):
        from git_viewspace.daemon import Daemon
        from unittest.mock import Mock

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            view = SimpleNamespace(
                path=root / "view", mountpoint=root / "public", routing={}
            )
            context = SimpleNamespace(
                runtime_entries=lambda: [],
                runtime=lambda: {
                    "daemon": {"pid": 11, "start_time_ticks": 12},
                    "viewfs": {
                        "source": "owned",
                        "supervisor_pid": 13,
                        "supervisor_start_time_ticks": 14,
                    },
                },
                mountpoint=root / "lower",
                socket_path=root / "socket",
                runtime_path=root / "runtime",
            )
            daemon = object.__new__(Daemon)
            daemon.context = context
            daemon.store = SimpleNamespace(views={"test": view})
            daemon.mounts = Mock()
            daemon.viewfs = Mock()
            daemon._remove_runtime_file = Mock()
            facts = {
                "private_mounted": True,
                "process_alive": False,
                "runtime": {"record_present": False, "mounted_paths": []},
            }
            with (
                patch("git_viewspace.daemon.mount_at", return_value=None),
                patch("git_viewspace.daemon.process_matches", return_value=False),
                patch("git_viewspace.routing.facts", return_value=facts),
            ):
                daemon.reconcile()
            daemon.mounts.cleanup_all.assert_called_once_with(
                daemon.store.views, "owned", force=True
            )

    def test_reconcile_refuses_unowned_private_router_residue(self):
        from git_viewspace.daemon import Daemon
        from unittest.mock import Mock

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            daemon = object.__new__(Daemon)
            daemon.context = SimpleNamespace(
                runtime_entries=lambda: [],
                runtime=lambda: None,
                mountpoint=root / "lower",
            )
            daemon.store = SimpleNamespace(
                views={
                    "test": SimpleNamespace(
                        path=root / "view", mountpoint=root / "public", routing={}
                    )
                }
            )
            daemon.mounts = Mock()
            facts = {
                "private_mounted": True,
                "process_alive": False,
                "runtime": {"record_present": False, "mounted_paths": []},
            }
            with (
                patch("git_viewspace.daemon.mount_at", return_value=None),
                patch("git_viewspace.routing.facts", return_value=facts),
            ):
                with self.assertRaises(GitViewspaceError) as failure:
                    daemon.reconcile()
            self.assertEqual(failure.exception.code, "FOREIGN_MOUNT")
            daemon.mounts.cleanup_all.assert_not_called()
