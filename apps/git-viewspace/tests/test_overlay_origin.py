import errno
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from git_viewspace.overlay_origin import ORIGIN, require_unmounted, reset_origins


class OriginResetTest(unittest.TestCase):
    def test_rejects_active_overlay_and_submount(self):
        root = Path("/scratch/upper")
        for info in [
            "1 0 0:1 / /view rw - overlay overlay rw,upperdir=/scratch/upper",
            "1 0 0:1 / /scratch/upper/child rw - tmpfs tmpfs rw",
        ]:
            with self.assertRaises(ValueError):
                require_unmounted(root, info)
        require_unmounted(root, "1 0 0:1 / / rw - ext4 /dev/root rw")
        with self.assertRaises(ValueError):
            require_unmounted(root, "malformed")

    def test_visits_symlinks_without_following_and_changes_only_origin(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            upper = root / "upper"
            upper.mkdir()
            external = root / "outside"
            external.mkdir()
            (external / "secret").write_text("unchanged")
            (upper / "file").write_text("content")
            (upper / "dir").mkdir()
            os.symlink(external, upper / "link-dir")
            os.symlink("file", upper / "link-file")
            calls = []

            def remove(path, key, *, follow_symlinks):
                self.assertEqual(key, ORIGIN)
                self.assertFalse(follow_symlinks)
                calls.append(Path(path).relative_to(upper).as_posix())
                if Path(path).name == "file":
                    raise OSError(errno.ENODATA, "absent")

            with patch(
                "git_viewspace.overlay_origin.os.removexattr", side_effect=remove
            ):
                result = reset_origins(upper)
            self.assertEqual(set(calls), {".", "file", "dir", "link-dir", "link-file"})
            self.assertEqual(result, {"visited": 5, "origins_removed": 4})
            self.assertEqual((upper / "file").read_text(), "content")
            self.assertEqual((external / "secret").read_text(), "unchanged")

    def test_failure_aborts_and_root_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            upper = root / "upper"
            upper.mkdir()
            os.symlink(upper, root / "alias")
            with self.assertRaises(ValueError):
                reset_origins(root / "alias")
            with patch(
                "git_viewspace.overlay_origin.os.removexattr",
                side_effect=PermissionError("denied"),
            ):
                with self.assertRaises(PermissionError):
                    reset_origins(upper)
