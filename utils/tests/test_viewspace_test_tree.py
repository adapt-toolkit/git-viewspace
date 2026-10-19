from __future__ import annotations

import os
from pathlib import Path
import shutil
import stat
import tempfile
import unittest
from unittest import mock
import zipfile

from utils import viewspace_test_tree


class TreeSupportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)

    def test_directory_and_zip_have_the_same_logical_snapshot(self) -> None:
        directory = self.base / "directory"
        (directory / "nested/empty").mkdir(parents=True)
        (directory / "nested/value.bin").write_bytes(b"\x00\xff")
        archive_path = self.base / "tree.zip"
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr("nested/", b"")
            archive.writestr("nested/empty/", b"")
            archive.writestr("nested/value.bin", b"\x00\xff")

        self.assertEqual(
            viewspace_test_tree.snapshot_source(directory),
            viewspace_test_tree.snapshot_source(archive_path),
        )

    def test_snapshot_ignores_non_executable_modes_and_timestamps(self) -> None:
        first = self.base / "first"
        second = self.base / "second"
        for root in (first, second):
            (root / "empty").mkdir(parents=True)
            (root / "value").write_bytes(b"same")
        (first / "empty").chmod(0o700)
        (second / "empty").chmod(0o755)
        (first / "value").chmod(0o600)
        (second / "value").chmod(0o644)
        os.utime(first / "value", ns=(1_000_000_000, 1_000_000_000))
        os.utime(second / "value", ns=(2_000_000_000, 2_000_000_000))

        self.assertEqual(
            viewspace_test_tree.snapshot_directory(first),
            viewspace_test_tree.snapshot_directory(second),
        )

    def test_cp437_zip_name_is_materialized_as_original_raw_bytes(self) -> None:
        archive_path = self.base / "raw-name.zip"
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_STORED) as archive:
            archive.writestr("x", b"value")
        archive_bytes = bytearray(archive_path.read_bytes())
        local = archive_bytes.index(b"PK\x03\x04")
        central = archive_bytes.index(b"PK\x01\x02")
        self.assertEqual(int.from_bytes(archive_bytes[local + 26 : local + 28], "little"), 1)
        self.assertEqual(int.from_bytes(archive_bytes[central + 28 : central + 30], "little"), 1)
        archive_bytes[local + 30] = 0xFF
        archive_bytes[central + 46] = 0xFF
        archive_path.write_bytes(archive_bytes)

        snapshot = viewspace_test_tree.snapshot_source(archive_path)

        self.assertEqual(snapshot, (viewspace_test_tree.TreeEntry(b"\xff", "file", b"value"),))

    def test_zip_rejects_raw_nul_in_entry_name(self) -> None:
        archive_path = self.base / "nul-name.zip"
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_STORED) as archive:
            archive.writestr("x", b"value")
        archive_bytes = bytearray(archive_path.read_bytes())
        local = archive_bytes.index(b"PK\x03\x04")
        central = archive_bytes.index(b"PK\x01\x02")
        archive_bytes[local + 30] = 0
        archive_bytes[central + 46] = 0
        archive_path.write_bytes(archive_bytes)

        with self.assertRaisesRegex(viewspace_test_tree.TreeError, "invalid name"):
            viewspace_test_tree.snapshot_source(archive_path)

    def test_zip_rejects_traversal_and_directory_payloads(self) -> None:
        cases: list[tuple[str, zipfile.ZipInfo, bytes]] = []
        cases.append(("traversal", zipfile.ZipInfo("../escape"), b"x"))
        directory_with_payload = zipfile.ZipInfo("directory/")
        directory_with_payload.create_system = 3
        directory_with_payload.external_attr = (stat.S_IFDIR | 0o755) << 16
        cases.append(("directory_payload", directory_with_payload, b"x"))

        for label, info, contents in cases:
            with self.subTest(label=label):
                archive_path = self.base / f"{label}.zip"
                with zipfile.ZipFile(archive_path, "w") as archive:
                    archive.writestr(info, contents)
                destination = self.base / f"{label}-destination"
                destination.mkdir()
                with self.assertRaises(viewspace_test_tree.TreeError):
                    viewspace_test_tree.extract_zip_safely(archive_path, destination)
                self.assertEqual(list(destination.iterdir()), [])
                self.assertFalse((self.base / "escape").exists())

    def test_failed_zip_extraction_cleans_partial_output(self) -> None:
        archive_path = self.base / "bad-crc.zip"
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_STORED) as archive:
            archive.writestr("bad.bin", b"payload")
        archive_bytes = bytearray(archive_path.read_bytes())
        local = archive_bytes.index(b"PK\x03\x04")
        name_length = int.from_bytes(archive_bytes[local + 26 : local + 28], "little")
        extra_length = int.from_bytes(archive_bytes[local + 28 : local + 30], "little")
        payload_offset = local + 30 + name_length + extra_length
        archive_bytes[payload_offset] ^= 0xFF
        archive_path.write_bytes(archive_bytes)
        destination = self.base / "destination"
        destination.mkdir()

        with self.assertRaises(viewspace_test_tree.TreeError):
            viewspace_test_tree.extract_zip_safely(archive_path, destination)

        self.assertEqual(list(destination.iterdir()), [])

    def test_zip_directory_record_validates_its_local_body(self) -> None:
        archive_path = self.base / "forged-directory.zip"
        directory = zipfile.ZipInfo("directory/")
        directory.create_system = 3
        directory.external_attr = (stat.S_IFDIR | 0o755) << 16
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_STORED) as archive:
            archive.writestr(directory, b"hidden payload")
        archive_bytes = bytearray(archive_path.read_bytes())
        central = archive_bytes.index(b"PK\x01\x02")
        archive_bytes[central + 24 : central + 28] = (0).to_bytes(4, "little")
        archive_path.write_bytes(archive_bytes)
        destination = self.base / "destination"
        destination.mkdir()

        with self.assertRaises(viewspace_test_tree.TreeError):
            viewspace_test_tree.extract_zip_safely(archive_path, destination)

        self.assertEqual(list(destination.iterdir()), [])

    def test_zip_extraction_requires_an_empty_destination_without_deleting_it(self) -> None:
        archive_path = self.base / "tree.zip"
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr("new", b"new")
        destination = self.base / "destination"
        destination.mkdir()
        (destination / "keep").write_bytes(b"keep")

        with self.assertRaisesRegex(viewspace_test_tree.TreeError, "not empty"):
            viewspace_test_tree.extract_zip_safely(archive_path, destination)

        self.assertEqual((destination / "keep").read_bytes(), b"keep")
        self.assertFalse((destination / "new").exists())

    def test_direct_tree_preserves_symlinks_and_rejects_special_nodes(self) -> None:
        symlink_root = self.base / "symlink-root"
        symlink_root.mkdir()
        (symlink_root / "link").symlink_to("target")
        self.assertEqual(
            viewspace_test_tree.snapshot_directory(symlink_root),
            (viewspace_test_tree.TreeEntry(b"link", "symlink", b"target"),),
        )

        fifo_root = self.base / "fifo-root"
        fifo_root.mkdir()
        os.mkfifo(fifo_root / "pipe")
        with self.assertRaisesRegex(viewspace_test_tree.TreeError, "only regular files"):
            viewspace_test_tree.snapshot_directory(fifo_root)

    def test_archive_round_trip_preserves_executable_files_and_symlinks(self) -> None:
        snapshot = (
            viewspace_test_tree.TreeEntry(b"bin", "directory"),
            viewspace_test_tree.TreeEntry(b"bin/tool", "file", b"#!/bin/sh\n", True),
            viewspace_test_tree.TreeEntry(b"current", "symlink", b"bin/tool"),
        )
        archive = self.base / "tree.zip"
        archive.write_bytes(viewspace_test_tree.snapshot_zip_bytes(snapshot))
        destination = self.base / "materialized"
        destination.mkdir()

        viewspace_test_tree.extract_zip_safely(archive, destination)

        self.assertEqual(viewspace_test_tree.snapshot_directory(destination), snapshot)
        self.assertTrue((destination / "bin/tool").stat().st_mode & stat.S_IXUSR)
        self.assertEqual(os.readlink(destination / "current"), "bin/tool")

    def test_snapshot_diff_reports_executable_bit_changes(self) -> None:
        expected = (viewspace_test_tree.TreeEntry(b"tool", "file", b"same", True),)
        actual = (viewspace_test_tree.TreeEntry(b"tool", "file", b"same", False),)

        difference = viewspace_test_tree.diff_snapshots(expected, actual)

        self.assertIn("- executable file b'tool'", difference)
        self.assertIn("+ non-executable file b'tool'", difference)

    def test_snapshot_never_follows_a_file_swapped_to_a_symlink(self) -> None:
        root = self.base / "root"
        root.mkdir()
        victim = root / "victim"
        victim.write_bytes(b"fixture")
        outside = self.base / "outside"
        outside.write_bytes(b"must not be captured")
        real_open = os.open
        swapped = False

        def raced_open(path, flags, mode=0o777, *, dir_fd=None):
            nonlocal swapped
            if path == b"victim" and dir_fd is not None and not flags & os.O_DIRECTORY:
                swapped = True
                victim.unlink()
                victim.symlink_to(outside)
            return real_open(path, flags, mode, dir_fd=dir_fd)

        with mock.patch.object(viewspace_test_tree.os, "open", side_effect=raced_open):
            with self.assertRaises(viewspace_test_tree.TreeError):
                viewspace_test_tree.snapshot_directory(root)

        self.assertTrue(swapped)

    def test_arbitrary_linux_filename_bytes_round_trip(self) -> None:
        source = self.base / "source"
        source.mkdir()
        raw_path = os.path.join(os.fsencode(source), b"\xff")
        descriptor = os.open(raw_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            os.write(descriptor, b"raw")
        finally:
            os.close(descriptor)
        snapshot = viewspace_test_tree.snapshot_directory(source)
        destination = self.base / "destination"
        destination.mkdir()

        viewspace_test_tree.materialize_snapshot(snapshot, destination)

        self.assertEqual(viewspace_test_tree.snapshot_directory(destination), snapshot)

    def test_deterministic_archive_writer_preserves_raw_path_bytes(self) -> None:
        snapshot = (
            viewspace_test_tree.TreeEntry(b"empty", "directory"),
            viewspace_test_tree.TreeEntry(b"\xff", "file", b"payload"),
        )

        first = viewspace_test_tree.snapshot_zip_bytes(snapshot)
        second = viewspace_test_tree.snapshot_zip_bytes(tuple(reversed(snapshot)))
        archive = self.base / "golden.zip"
        archive.write_bytes(first)

        self.assertEqual(first, second)
        self.assertEqual(viewspace_test_tree.snapshot_source(archive), snapshot)

    def test_materialization_cleans_partial_output_on_failure(self) -> None:
        destination = self.base / "destination"
        destination.mkdir()
        snapshot = (
            viewspace_test_tree.TreeEntry(b"nested", "directory"),
            viewspace_test_tree.TreeEntry(b"nested/value", "file", b"value"),
        )

        with mock.patch("builtins.open", side_effect=OSError("injected failure")):
            with self.assertRaises(viewspace_test_tree.TreeError):
                viewspace_test_tree.materialize_snapshot(snapshot, destination)

        self.assertEqual(list(destination.iterdir()), [])

    def test_snapshot_diff_rejects_duplicates_instead_of_masking_them(self) -> None:
        duplicate = (
            viewspace_test_tree.TreeEntry(b"value", "file", b"first"),
            viewspace_test_tree.TreeEntry(b"value", "file", b"second"),
        )
        with self.assertRaisesRegex(viewspace_test_tree.TreeError, "duplicate"):
            viewspace_test_tree.diff_snapshots(duplicate, duplicate)

    def test_snapshot_diff_reports_exact_content_and_shape_changes(self) -> None:
        expected = (
            viewspace_test_tree.TreeEntry(b"empty", "directory"),
            viewspace_test_tree.TreeEntry(b"value", "file", b"old"),
        )
        actual = (
            viewspace_test_tree.TreeEntry(b"added", "file", b"new"),
            viewspace_test_tree.TreeEntry(b"value", "file", b"changed"),
        )

        difference = viewspace_test_tree.diff_snapshots(expected, actual)

        self.assertIn("- directory b'empty'", difference)
        self.assertIn("+ file b'added'", difference)
        self.assertIn("--- expected/b'value'", difference)
        self.assertIn("-old", difference)
        self.assertIn("+changed", difference)

    def test_snapshot_diff_reports_bounded_binary_offset_and_digests(self) -> None:
        expected = (viewspace_test_tree.TreeEntry(b"blob", "file", b"a\x00b"),)
        actual = (viewspace_test_tree.TreeEntry(b"blob", "file", b"a\x00c"),)

        difference = viewspace_test_tree.diff_snapshots(expected, actual)

        self.assertIn("first differing offset: 2", difference)
        self.assertEqual(difference.count("sha256="), 2)
        self.assertIn("610062", difference)
        self.assertIn("610063", difference)

    def test_atomic_directory_replacement_is_complete_and_rolls_back_population_failure(
        self,
    ) -> None:
        target = self.base / "golden"
        target.mkdir()
        (target / "old").write_text("old", encoding="utf-8")

        def populate(directory: Path) -> None:
            (directory / "new").write_text("new", encoding="utf-8")

        viewspace_test_tree.atomic_replace_directory(target, populate)
        self.assertEqual([path.name for path in target.iterdir()], ["new"])

        def fail(directory: Path) -> None:
            (directory / "partial").write_text("partial", encoding="utf-8")
            raise RuntimeError("stop")

        with self.assertRaisesRegex(RuntimeError, "stop"):
            viewspace_test_tree.atomic_replace_directory(target, fail)
        self.assertEqual((target / "new").read_text(encoding="utf-8"), "new")
        self.assertEqual(
            sorted(path.name for path in self.base.iterdir() if path.name.startswith(".golden.")),
            [],
        )

    def test_atomic_directory_replacement_rejects_non_directory_target(self) -> None:
        target = self.base / "golden"
        target.write_text("do not replace", encoding="utf-8")

        with self.assertRaisesRegex(viewspace_test_tree.TreeError, "not a directory"):
            viewspace_test_tree.atomic_replace_directory(target, lambda _: None)

        self.assertEqual(target.read_text(encoding="utf-8"), "do not replace")


if __name__ == "__main__":
    unittest.main()
