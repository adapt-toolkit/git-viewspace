#!/usr/bin/python3
"""Unprivileged classifier, atomicity, and comparator regressions."""

from __future__ import annotations

import importlib.util
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


HERE = Path(__file__).resolve().parent


def load_module(name: str, path: Path):
    specification = importlib.util.spec_from_file_location(name, path)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    sys.modules[name] = module
    specification.loader.exec_module(module)
    return module


SYNC = load_module(
    "viewspace_overlay_sync",
    HERE.parents[1] / "git_viewspace/overlay_sync.py",
)
HARNESS = load_module("viewspace_overlay_sync_harness", HERE / "run-tests.py")


class OverlaySyncUnitTests(unittest.TestCase):
    def test_upper_fingerprint_binds_regular_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            upper = Path(temporary)
            payload = upper / "payload"
            payload.write_bytes(b"first\0value")
            first_inventory = SYNC.inventory_upper(upper)
            first = SYNC.inventory_fingerprint(upper, first_inventory)
            payload.write_bytes(b"other\0value")
            second_inventory = SYNC.inventory_upper(upper)
            second = SYNC.inventory_fingerprint(upper, second_inventory)
            self.assertNotEqual(first, second)

    def test_owner_unreadable_regular_is_rejected_before_replay(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            upper = Path(temporary)
            payload = upper / "payload"
            payload.write_text("unreadable\n", encoding="utf-8")
            payload.chmod(0)
            with self.assertRaisesRegex(SYNC.SyncError, "owner-readable"):
                SYNC.inventory_upper(upper)

    def test_origin_accepts_only_qualified_fuse_and_ext4_profiles(self) -> None:
        fuse = bytes.fromhex(
            "00fb2100810000000000000000000000000000000000000000"
            "0d00000000000000"
        )
        ext4 = bytes.fromhex(
            "00fb1d0001a04243ecb7c44a25bc952d1dc1c1b8e5"
            "37b40200520f0163"
        )
        SYNC._validate_origin(fuse, "fuse")
        SYNC._validate_origin(ext4, "ext4")
        malformed = (
            bytes.fromhex("00fb060001") + b"x"
        )
        with self.assertRaises(SYNC.SyncError):
            SYNC._validate_origin(malformed, "bad")

    def test_directory_may_be_both_opaque_and_impure(self) -> None:
        SYNC._validate_xattrs(
            "directory",
            {SYNC.OPAQUE: b"y", SYNC.IMPURE: b"y"},
            "moved-into-directory",
        )

    def test_git_execute_class_uses_owner_bit_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            destination = root / "destination"
            source.write_text("payload\n", encoding="utf-8")
            source.chmod(0o655)
            SYNC._copy_git_tree(source, destination)
            self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o644)

    def test_manifest_rejects_symlink_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            real = root / "real"
            real.mkdir()
            (real / "file").write_text("payload\n", encoding="utf-8")
            link = root / "link"
            link.symlink_to(real, target_is_directory=True)
            with self.assertRaises(HARNESS.TestFailure):
                HARNESS.git_manifest(link)

    def test_hardlinked_target_fails_without_change_or_residue(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            upper = root / "upper"
            target = root / "target"
            upper.mkdir()
            target.mkdir()
            first = target / "first"
            first.write_text("same inode\n", encoding="utf-8")
            os.link(first, target / "second")
            before = sorted(path.name for path in target.iterdir())
            with self.assertRaises(SYNC.SyncError):
                SYNC.sync_overlay(upper, target, atomic_swap=True)
            self.assertEqual(sorted(path.name for path in target.iterdir()), before)
            self.assertEqual(list(root.glob(".target.overlay-sync-*")), [])

    def test_direct_mode_preserves_checkout_dot_git(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            upper = root / "upper"
            target = root / "target"
            upper.mkdir()
            (target / ".git").mkdir(parents=True)
            marker = target / ".git" / "config"
            marker.write_text("do not touch\n", encoding="utf-8")
            result = SYNC.sync_overlay(upper, target)
            self.assertEqual(marker.read_text(encoding="utf-8"), "do not touch\n")
            self.assertEqual(result["mode"], "direct")

    def test_root_privilege_drop_clears_groups_then_sets_gid_and_uid(self) -> None:
        calls: list[tuple[str, object]] = []
        with (
            mock.patch.object(SYNC.os, "geteuid", return_value=0),
            mock.patch.object(SYNC.os, "getegid", return_value=0),
            mock.patch.object(
                SYNC.os,
                "setgroups",
                side_effect=lambda groups: calls.append(("groups", groups)),
            ),
            mock.patch.object(
                SYNC.os,
                "setgid",
                side_effect=lambda gid: calls.append(("gid", gid)),
            ),
            mock.patch.object(
                SYNC.os,
                "setuid",
                side_effect=lambda uid: calls.append(("uid", uid)),
            ),
        ):
            SYNC._drop_to_workload(12345, 23456)
        self.assertEqual(
            calls,
            [("groups", []), ("gid", 23456), ("uid", 12345)],
        )

    def test_root_owned_upper_cannot_replay_as_root(self) -> None:
        with self.assertRaisesRegex(SYNC.SyncError, "non-root workload user"):
            SYNC._drop_to_workload(0, 0)

    def test_replay_begins_only_after_privilege_drop(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            upper = root / "upper"
            target = root / "target"
            upper.mkdir()
            target.mkdir()
            inventory = SYNC.inventory_upper(upper)
            calls: list[str] = []

            def drop(uid: int, gid: int) -> None:
                self.assertEqual(
                    (uid, gid),
                    (inventory.ordinary_uid, inventory.ordinary_gid),
                )
                calls.append("drop")

            def replay(*_args: object) -> bool:
                calls.append("replay")
                return False

            with (
                mock.patch.object(SYNC, "inventory_upper", return_value=inventory),
                mock.patch.object(SYNC, "_drop_to_workload", side_effect=drop),
                mock.patch.object(SYNC, "_sync_direct", side_effect=replay),
            ):
                SYNC.sync_overlay(upper, target)
            self.assertEqual(calls, ["drop", "replay"])

            with (
                mock.patch.object(SYNC, "inventory_upper", return_value=inventory),
                mock.patch.object(
                    SYNC,
                    "_drop_to_workload",
                    side_effect=SYNC.SyncError("injected drop failure"),
                ),
                mock.patch.object(SYNC, "_sync_direct") as replay_after_failure,
            ):
                with self.assertRaisesRegex(SYNC.SyncError, "injected drop failure"):
                    SYNC.sync_overlay(upper, target)
            replay_after_failure.assert_not_called()

    def test_emitted_entries_are_naturally_workload_owned_in_both_modes(self) -> None:
        def portable_exchange(left: Path, right: Path) -> None:
            holding = left.parent / "exchange-holding"
            left.rename(holding)
            right.rename(left)
            holding.rename(right)

        for atomic_swap in (False, True):
            with (
                self.subTest(atomic_swap=atomic_swap),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = Path(temporary)
                upper = root / "upper"
                target = root / "target"
                (upper / "directory").mkdir(parents=True)
                target.mkdir()
                (upper / "regular").write_text("payload\n", encoding="utf-8")
                (upper / "symlink").symlink_to("regular")
                (upper / "directory" / "child").write_text("child\n", encoding="utf-8")
                with mock.patch.object(
                    SYNC, "_rename_exchange", side_effect=portable_exchange
                ):
                    SYNC.sync_overlay(upper, target, atomic_swap=atomic_swap)

                expected = os.geteuid(), os.getegid()
                for relative in ("regular", "symlink", "directory", "directory/child"):
                    info = (target / relative).lstat()
                    self.assertEqual((info.st_uid, info.st_gid), expected)

    def test_publication_failure_leaves_target_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            upper = root / "upper"
            target = root / "target"
            upper.mkdir()
            target.mkdir()
            (upper / "new").write_text("new\n", encoding="utf-8")
            old = target / "old"
            old.write_text("old\n", encoding="utf-8")
            with mock.patch.object(
                SYNC, "_rename_exchange", side_effect=SYNC.SyncError("injected")
            ):
                with self.assertRaises(SYNC.SyncError):
                    SYNC.sync_overlay(upper, target, atomic_swap=True)
            self.assertEqual(old.read_text(encoding="utf-8"), "old\n")
            self.assertFalse((target / "new").exists())
            self.assertEqual(list(root.glob(".target.overlay-sync-*")), [])

    def test_cross_redirect_overlap_is_rejected(self) -> None:
        def whiteout(parts: tuple[str, ...]):
            return SYNC.Entry(
                parts,
                "whiteout",
                stat.S_IFCHR,
                0,
                0,
                1,
                0,
                {},
            )

        def redirect(parts: tuple[str, ...], source: tuple[str, ...]):
            return SYNC.Entry(
                parts,
                "directory",
                stat.S_IFDIR | 0o755,
                os.getuid(),
                os.getgid(),
                1,
                0,
                {},
                redirect_source=source,
            )

        left = redirect(("X",), ("A",))
        right = redirect(("Y",), ("X", "child"))
        by_path = {
            ("A",): whiteout(("A",)),
            ("X", "child"): whiteout(("X", "child")),
        }
        with self.assertRaises(SYNC.SyncError):
            SYNC._validate_redirect_set(by_path, (left, right))

    def test_git_pathspecs_are_minimal_semantic_roots(self) -> None:
        def entry(
            parts: tuple[str, ...],
            kind: str,
            *,
            opaque: bool = False,
            redirect_source: tuple[str, ...] | None = None,
        ):
            return SYNC.Entry(
                parts,
                kind,
                stat.S_IFDIR | 0o755 if kind == "directory" else stat.S_IFREG | 0o644,
                os.getuid(),
                os.getgid(),
                1,
                0,
                {SYNC.OPAQUE: b"y"} if opaque else {},
                redirect_source=redirect_source,
            )

        root = entry((), "directory")
        records = (
            root,
            entry(("ordinary-parent",), "directory"),
            entry(("ordinary-parent", "file"), "regular"),
            entry(("deleted",), "whiteout"),
            entry(("opaque",), "directory", opaque=True),
            entry(("opaque", "child"), "regular"),
            entry(("moved",), "directory", redirect_source=("source",)),
            entry(("moved", "changed"), "regular"),
            entry(("source",), "whiteout"),
        )
        inventory = SYNC.Inventory(
            root,
            {record.parts: record for record in records},
            (records[6],),
            os.getuid(),
            os.getgid(),
        )
        self.assertEqual(
            SYNC.git_pathspecs(inventory),
            ("deleted", "moved", "opaque", "ordinary-parent/file", "source"),
        )
        # Publication-sized sibling sets must examine ancestors, not all
        # previous siblings. Count work rather than asserting wall-clock time.
        class CountedPath(tuple):
            slices = 0

            def __getitem__(self, key):
                if isinstance(key, slice):
                    type(self).slices += 1
                return super().__getitem__(key)

        siblings = [
            entry(CountedPath(("dataset", "records", str(i))), "regular")
            for i in range(1000)
        ]
        inventory = SYNC.Inventory(
            root, {item.parts: item for item in siblings}, (), os.getuid(), os.getgid()
        )
        self.assertEqual(
            SYNC.git_pathspecs(inventory),
            tuple(sorted("/".join(item.parts) for item in siblings)),
        )
        self.assertLessEqual(CountedPath.slices, 3 * len(siblings))

    def test_git_pathspecs_handle_empty_and_root_opaque(self) -> None:
        root = SYNC.Entry(
            (),
            "directory",
            stat.S_IFDIR | 0o755,
            os.getuid(),
            os.getgid(),
            1,
            0,
            {},
        )
        empty = SYNC.Inventory(root, {(): root}, (), os.getuid(), os.getgid())
        self.assertEqual(SYNC.git_pathspecs(empty), ())

        opaque = SYNC.Entry(
            (),
            "directory",
            stat.S_IFDIR | 0o755,
            os.getuid(),
            os.getgid(),
            1,
            0,
            {SYNC.OPAQUE: b"y"},
        )
        child = SYNC.Entry(
            (":literal*\n",),
            "regular",
            stat.S_IFREG | 0o644,
            os.getuid(),
            os.getgid(),
            1,
            0,
            {},
        )
        inventory = SYNC.Inventory(
            opaque,
            {(): opaque, child.parts: child},
            (),
            os.getuid(),
            os.getgid(),
        )
        self.assertEqual(SYNC.git_pathspecs(inventory), (".",))


if __name__ == "__main__":
    unittest.main()
