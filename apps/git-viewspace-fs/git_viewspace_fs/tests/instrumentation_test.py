# SPDX-License-Identifier: Apache-2.0

import json
import os
import stat
import tempfile
from pathlib import Path
from unittest import TestCase, main

from git_viewspace_fs.instrumentation import JsonlInstrumentation


class JsonlInstrumentationTestCase(TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.log_path = Path(self.temporary_directory.name) / "trace.jsonl"
        self.log_path.touch(mode=0o600)
        self.source = "GitViewspaceFS:root-1:attempt-1"

    def instrumentation(self, path=None):
        return JsonlInstrumentation(
            path or self.log_path,
            self.source,
            os.getuid(),
            os.getgid(),
        )

    def records(self):
        return [
            json.loads(line)
            for line in self.log_path.read_text(encoding="utf-8").splitlines()
        ]

    def test_events_are_ordered_counted_and_source_identified(self):
        instrumentation = self.instrumentation()
        instrumentation.emit("lookup", path="/one")
        instrumentation.emit("lookup", path="/two")
        instrumentation.emit("read", size=7)
        instrumentation.close()

        records = self.records()
        self.assertEqual([record["sequence"] for record in records], [1, 2, 3])
        self.assertEqual(
            [record["event_count"] for record in records],
            [1, 2, 1],
        )
        self.assertEqual(
            [record["event"] for record in records],
            ["lookup", "lookup", "read"],
        )
        self.assertTrue(
            all(record["filesystem_source"] == self.source for record in records)
        )
        self.assertEqual(records[0]["path"], "/one")
        self.assertEqual(records[2]["size"], 7)
        self.assertEqual(stat.S_IMODE(self.log_path.stat().st_mode), 0o600)

    def test_nonempty_and_multiply_linked_logs_are_rejected(self):
        self.log_path.write_text('{"prior":true}\n', encoding="utf-8")
        with self.assertRaises(ValueError):
            self.instrumentation()

        self.log_path.write_bytes(b"")
        second_link = Path(self.temporary_directory.name) / "second-link"
        os.link(self.log_path, second_link)
        with self.assertRaises(ValueError):
            self.instrumentation()

    def test_owner_identity_and_owner_write_are_required(self):
        with self.assertRaises(ValueError):
            JsonlInstrumentation(
                self.log_path,
                self.source,
                os.getuid() + 1,
                os.getgid(),
            )
        with self.assertRaises(ValueError):
            JsonlInstrumentation(
                self.log_path,
                self.source,
                os.getuid(),
                os.getgid() + 1,
            )

        self.log_path.chmod(0o400)
        with self.assertRaises((OSError, ValueError)):
            self.instrumentation()

    def test_non_regular_and_symlink_log_targets_are_rejected(self):
        directory = Path(self.temporary_directory.name) / "directory"
        directory.mkdir()
        with self.assertRaises(OSError):
            self.instrumentation(directory)

        target = Path(self.temporary_directory.name) / "target"
        target.touch()
        symlink = Path(self.temporary_directory.name) / "symlink"
        symlink.symlink_to(target)
        with self.assertRaises(OSError):
            self.instrumentation(symlink)

        missing = Path(self.temporary_directory.name) / "missing"
        with self.assertRaises(FileNotFoundError):
            self.instrumentation(missing)

        fifo = Path(self.temporary_directory.name) / "fifo"
        os.mkfifo(fifo)
        with self.assertRaises(OSError):
            self.instrumentation(fifo)

    def test_close_is_idempotent_and_further_events_are_rejected(self):
        instrumentation = self.instrumentation()
        instrumentation.close()
        instrumentation.close()

        with self.assertRaises(ValueError):
            instrumentation.emit("late")


if __name__ == "__main__":
    main()
