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

    def test_missing_backing_preserves_admitted_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            managed = root / "managed"
            managed.mkdir()
            base = managed / "base"
            base.mkdir()
            viewpath = managed / "view"
            (viewpath / "routing").mkdir(parents=True)
            external = root / "external"
            external.write_text("external-only")
            (base / (PREFIX + "credential")).write_text(str(external) + "\n")
            view = SimpleNamespace(path=viewpath, commit="a" * 40)
            context = SimpleNamespace(
                root=managed, repository=managed, uid=os.getuid(), gid=os.getgid()
            )
            mounts = SimpleNamespace(context=context)
            with patch("git_viewspace.submodules.entries", return_value={}):
                _table(mounts, view, base)
                original = (viewpath / "routing/bindings.json").read_bytes()
                external.unlink()
                _table(mounts, view, base)
                self.assertEqual(
                    original, (viewpath / "routing/bindings.json").read_bytes()
                )
                external.write_text("replacement")
                _table(mounts, view, base)
                self.assertEqual(
                    original, (viewpath / "routing/bindings.json").read_bytes()
                )
                (base / "credential").write_text("collision")
                with self.assertRaises(GitViewspaceError):
                    _table(mounts, view, base)

    def test_descriptor_directory_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / (PREFIX + "invalid")).mkdir()
            from git_viewspace.routing import _bindings

            with patch("git_viewspace.submodules.entries", return_value={}):
                with self.assertRaises(GitViewspaceError):
                    _bindings(
                        SimpleNamespace(
                            context=SimpleNamespace(root=base, repository=base)
                        ),
                        SimpleNamespace(commit="a" * 40),
                        base,
                    )

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
