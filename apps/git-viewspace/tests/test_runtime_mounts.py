import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from git_viewspace.errors import GitViewspaceError
from git_viewspace.runtime_mounts import validate_mapping, read_record, identity


class RuntimeMountTest(unittest.TestCase):
    def test_mapping_rejects_ambiguous_paths(self):
        validate_mapping({"runtime": "/external", "cache": "/cache"})
        for value in [
            [],
            {"": "/x"},
            {"/root": "/x"},
            {"a/../b": "/x"},
            {"a//b": "/x"},
            {"a": "relative"},
            {"a": "/x", "a-else": "/y", "a/b": "/z"},
            {"a/.git/b": "/x"},
        ]:
            with self.assertRaises(GitViewspaceError):
                validate_mapping(value)

    def test_malformed_durable_record_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "routing").mkdir()
            record = root / "routing/runtime-mounts.json"
            record.write_text(json.dumps([{"path": "../../other"}]))
            record.chmod(0o600)
            with self.assertRaises(GitViewspaceError):
                read_record(SimpleNamespace(path=root))

    def test_foreign_mount_id_is_rejected_before_access(self):
        view = SimpleNamespace(mountpoint=Path("/example"))
        item = dict(
            path="runtime",
            filesystem="ext4",
            root="/external",
            mount_id=123,
            device=1,
            inode=2,
        )
        with patch(
            "git_viewspace.runtime_mounts.mount_at",
            return_value=SimpleNamespace(
                filesystem_type="ext4", root="/external", mount_id=124
            ),
        ):
            with self.assertRaises(GitViewspaceError):
                identity(view, item)

    def test_failed_remount_retains_ownership_and_busy_stop_retains_record(self):
        from unittest.mock import Mock
        from git_viewspace.runtime_mounts import start, stop, record_path

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "routing").mkdir()
            view = SimpleNamespace(
                path=root,
                mountpoint=root / "mnt",
                kind="overlay",
                routing={"runtime_directories": {"runtime": "/external"}},
            )
            item = dict(
                path="runtime",
                source="/external",
                device=1,
                inode=2,
                filesystem="ext4",
                root="/external",
            )
            mounts = Mock(sudo="sudo", mount="mount")
            mounts._command.side_effect = [
                None,
                GitViewspaceError("RUNTIME_MOUNT_FAILED", "remount failed"),
            ]
            with (
                patch("git_viewspace.runtime_mounts.mount_at", return_value=None),
                patch(
                    "git_viewspace.runtime_mounts.identity",
                    return_value=SimpleNamespace(mount_id=123),
                ),
            ):
                with self.assertRaises(GitViewspaceError):
                    start(mounts, view, [item])
                original = record_path(view).read_bytes()
                self.assertEqual(read_record(view), [dict(item, mount_id=123)])
                mounts._unmount.side_effect = GitViewspaceError("BUSY", "busy mount")
                with self.assertRaises(GitViewspaceError):
                    stop(mounts, view)
                self.assertEqual(record_path(view).read_bytes(), original)
                mounts._unmount.side_effect = None
                stop(mounts, view)
                self.assertFalse(record_path(view).exists())

    def test_unrecorded_runtime_mount_is_never_unmounted(self):
        from unittest.mock import Mock
        from git_viewspace.runtime_mounts import stop

        with tempfile.TemporaryDirectory() as tmp:
            view = SimpleNamespace(
                path=Path(tmp),
                mountpoint=Path(tmp) / "mnt",
                routing={"runtime_directories": {"runtime": "/external"}},
            )
            mounts = Mock()
            with patch("git_viewspace.runtime_mounts.mount_at", return_value=object()):
                with self.assertRaises(GitViewspaceError):
                    stop(mounts, view, force=True)
            mounts._unmount.assert_not_called()
