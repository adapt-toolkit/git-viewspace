#!/usr/bin/env python
# SPDX-License-Identifier: Apache-2.0

import errno
import json
import os
import shutil
import tempfile
import threading
from pathlib import Path
from stat import S_IFIFO
from types import SimpleNamespace
from unittest import TestCase, main
from unittest.mock import patch

from pygit2 import Commit, Repository, Signature
from pygit2.enums import ObjectType

from git_viewspace_fs.gitoper import GitOperError, GitOperations
from git_viewspace_fs.view_config import ViewRegistry


COMMIT_A = "a" * 40
COMMIT_B = "b" * 40


class FakeGitOperations:
    """Small exact-commit oracle for tests concerned with configuration."""

    def __init__(self, commits=(COMMIT_A, COMMIT_B)):
        self.commits = set(commits)
        self.calls = []

    def resolve_commit(self, object_id):
        self.calls.append(object_id)
        if object_id not in self.commits:
            raise GitOperError(errno.ENOENT, "not an exact commit")
        return object_id


class ObservedLock:
    """An RLock that lets a test observe attempts without timing guesses."""

    def __init__(self):
        self._lock = threading.RLock()
        self._count_lock = threading.Lock()
        self.attempts = 0
        self.second_attempt = threading.Event()

    def __enter__(self):
        with self._count_lock:
            self.attempts += 1
            if self.attempts == 2:
                self.second_attempt.set()
        self._lock.acquire()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self._lock.release()


class BlockingGitOperations(FakeGitOperations):
    """Hold the first validation while another caller reaches the lock."""

    def __init__(self):
        super().__init__((COMMIT_A, COMMIT_B))
        self.entered = threading.Event()
        self.release = threading.Event()

    def resolve_commit(self, object_id):
        self.calls.append(object_id)
        self.entered.set()
        if not self.release.wait(5):
            raise RuntimeError("timed out waiting to release commit validation")
        if object_id not in self.commits:
            raise GitOperError(errno.ENOENT, "not an exact commit")
        return object_id


class ViewRegistryTestCase(TestCase):
    def setUp(self):
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.directory = Path(self._temporary_directory.name)
        self.config_path = self.directory / "views.json"
        self.git = FakeGitOperations()

    def tearDown(self):
        self._temporary_directory.cleanup()

    def registry(self, git_operations=None, config_path=None):
        return ViewRegistry(
            str(config_path or self.config_path),
            git_operations or self.git,
        )

    def write_config(self, value):
        self.config_path.write_text(json.dumps(value), encoding="utf-8")

    def write_raw(self, value):
        self.config_path.write_text(value, encoding="utf-8")

    def assert_invalid_snapshot(self, raw=None, value=None, alias="good"):
        if raw is not None:
            self.write_raw(raw)
        else:
            self.write_config(value)
        registry = self.registry()
        self.assertIsNone(registry.resolve(alias))
        self.assertEqual(registry.names(), [])

    def test_config_does_not_have_to_exist_at_initialization(self):
        registry = self.registry()

        self.assertTrue(Path(registry.config_path).is_absolute())
        self.assertIsNone(registry.resolve("later"))
        self.assertEqual(registry.names(), [])

        self.write_config({"later": {"commit": COMMIT_A}})
        self.assertEqual(registry.resolve("later")["commit"], COMMIT_A)

    def test_empty_snapshot_and_missing_name_are_not_errors_or_pins(self):
        self.write_config({})
        registry = self.registry()

        self.assertIsNone(registry.resolve("missing"))
        self.assertEqual(registry.names(), [])

        self.write_config({"other": {"commit": COMMIT_A}})
        self.assertIsNone(registry.resolve("missing"))
        self.assertEqual(registry.names(), ["other"])

        self.write_config({"missing": {"commit": COMMIT_B}})
        self.assertEqual(registry.resolve("missing")["commit"], COMMIT_B)

    def test_relative_config_path_is_made_absolute_at_initialization(self):
        relative_path = os.path.relpath(self.config_path)
        registry = ViewRegistry(relative_path, self.git)

        self.assertEqual(registry.config_path, str(self.config_path))
        self.write_config({"view": {"commit": COMMIT_A}})
        self.assertEqual(registry.resolve("view")["commit"], COMMIT_A)

    def test_regular_file_is_accepted_and_symlink_to_it_is_accepted(self):
        target = self.directory / "actual.json"
        target.write_text(
            json.dumps({"view": {"commit": COMMIT_A}}),
            encoding="utf-8",
        )
        self.config_path.symlink_to(target.name)

        self.assertEqual(self.registry().resolve("view")["commit"], COMMIT_A)

    def test_retargeting_config_symlink_is_seen_by_an_unpinned_lookup(self):
        first_target = self.directory / "first.json"
        first_target.write_text(
            json.dumps({"view": {"commit": COMMIT_A}}),
            encoding="utf-8",
        )
        second_target = self.directory / "second.json"
        second_target.write_text(
            json.dumps({"view": {"commit": COMMIT_B}}),
            encoding="utf-8",
        )
        self.config_path.symlink_to(first_target.name)
        registry = self.registry()
        self.assertEqual(registry.names(), ["view"])

        replacement_link = self.directory / "replacement-link"
        replacement_link.symlink_to(second_target.name)
        os.replace(replacement_link, self.config_path)

        self.assertEqual(registry.resolve("view")["commit"], COMMIT_B)

    def test_replacing_config_file_is_seen_by_an_unpinned_lookup(self):
        self.write_config({"view": {"commit": COMMIT_A}})
        registry = self.registry()
        self.assertEqual(registry.names(), ["view"])

        replacement = self.directory / "replacement.json"
        replacement.write_text(
            json.dumps({"view": {"commit": COMMIT_B}}),
            encoding="utf-8",
        )
        os.replace(replacement, self.config_path)

        self.assertEqual(registry.resolve("view")["commit"], COMMIT_B)

    def test_directory_is_not_treated_as_configuration(self):
        self.config_path.mkdir()
        registry = self.registry()

        self.assertIsNone(registry.resolve("view"))
        self.assertEqual(registry.names(), [])

    def test_nonregular_descriptor_is_rejected_before_valid_json_is_read(self):
        self.write_config({"view": {"commit": COMMIT_A}})
        registry = self.registry()

        # Model an opened FIFO without creating a blocking kernel object.  The
        # descriptor contains valid JSON, so only the fstat type check can make
        # this snapshot invalid.
        with patch(
            "git_viewspace_fs.view_config.os.fstat",
            return_value=SimpleNamespace(st_mode=S_IFIFO | 0o600),
        ):
            self.assertIsNone(registry.resolve("view"))
            self.assertEqual(registry.names(), [])

    def test_unreadable_file_is_equivalent_to_missing_alias(self):
        self.write_config({"view": {"commit": COMMIT_A}})
        registry = self.registry()

        with patch(
            "git_viewspace_fs.view_config.os.open",
            side_effect=PermissionError(errno.EACCES, "permission denied"),
        ):
            self.assertIsNone(registry.resolve("view"))
            self.assertEqual(registry.names(), [])

    def test_post_open_read_error_is_equivalent_to_missing_alias(self):
        self.write_config({"view": {"commit": COMMIT_A}})
        registry = self.registry()

        with patch(
            "git_viewspace_fs.view_config.json.load",
            side_effect=OSError(errno.EIO, "read failed"),
        ):
            self.assertIsNone(registry.resolve("view"))
            self.assertEqual(registry.names(), [])

    def test_open_uses_nonblocking_mode(self):
        self.write_config({"view": {"commit": COMMIT_A}})
        registry = self.registry()

        with patch(
            "git_viewspace_fs.view_config.os.open",
            wraps=os.open,
        ) as mocked_open:
            self.assertEqual(registry.resolve("view")["commit"], COMMIT_A)

        flags = mocked_open.call_args.args[1]
        self.assertTrue(flags & os.O_NONBLOCK)

    def test_invalid_utf8_is_an_invalid_snapshot(self):
        self.config_path.write_bytes(
            b'{"v\xff":{"commit":"' + b"a" * 40 + b'"}}'
        )
        registry = self.registry()

        # Replacement and ignore decoding would each produce a valid alias.
        self.assertIsNone(registry.resolve("v\ufffd"))
        self.assertIsNone(registry.resolve("v"))
        self.assertEqual(registry.names(), [])

    def test_malformed_and_trailing_json_are_transactionally_invalid(self):
        documents = (
            "",
            "{",
            '{"good":{"commit":"' + COMMIT_A + '"}',
            json.dumps({"good": {"commit": COMMIT_A}}) + " garbage",
        )
        for document in documents:
            with self.subTest(document=document):
                self.assert_invalid_snapshot(raw=document)

    def test_json_recursion_limit_failure_is_transactionally_invalid(self):
        self.write_config({"good": {"commit": COMMIT_A}})
        registry = self.registry()

        with patch(
            "git_viewspace_fs.view_config.json.load",
            side_effect=RecursionError("JSON nesting is too deep"),
        ):
            self.assertIsNone(registry.resolve("good"))
            self.assertEqual(registry.names(), [])

    def test_json_integer_limit_failure_is_transactionally_invalid(self):
        self.write_config({"good": {"commit": COMMIT_A}})
        registry = self.registry()

        with patch(
            "git_viewspace_fs.view_config.json.load",
            side_effect=ValueError("integer string exceeds conversion limit"),
        ):
            self.assertIsNone(registry.resolve("good"))
            self.assertEqual(registry.names(), [])

    def test_duplicate_view_name_invalidates_entire_snapshot(self):
        document = (
            '{"duplicate":{"commit":"'
            + COMMIT_A
            + '"},"duplicate":{"commit":"'
            + COMMIT_B
            + '"}}'
        )

        self.assert_invalid_snapshot(raw=document, alias="duplicate")

    def test_duplicate_parameter_name_invalidates_entire_snapshot(self):
        document = (
            '{"good":{"commit":"'
            + COMMIT_A
            + '","commit":"'
            + COMMIT_B
            + '"}}'
        )

        self.assert_invalid_snapshot(raw=document)

    def test_top_level_must_be_an_object(self):
        for value in (None, [], "text", 1, True):
            with self.subTest(value=value):
                self.assert_invalid_snapshot(value=value)

    def test_view_parameters_have_a_strict_schema(self):
        invalid_parameters = (
            None,
            [],
            COMMIT_A,
            {},
            {"other": COMMIT_A},
            {"commit": COMMIT_A, "future": True},
            {"commit": None},
            {"commit": True},
            {"commit": 1},
            {"commit": []},
            {"commit": {}},
            {"commit": ""},
        )
        for parameters in invalid_parameters:
            with self.subTest(parameters=parameters):
                self.assert_invalid_snapshot(
                    value={
                        "good": {"commit": COMMIT_A},
                        "bad": parameters,
                    }
                )

    def test_one_unknown_commit_invalidates_valid_siblings(self):
        self.write_config(
            {
                "good": {"commit": COMMIT_A},
                "bad": {"commit": "f" * 40},
            }
        )
        registry = self.registry()

        self.assertIsNone(registry.resolve("good"))
        self.assertEqual(registry.names(), [])

    def test_aliases_must_be_single_safe_utf8_components(self):
        invalid_aliases = (
            "",
            ".",
            "..",
            "with/slash",
            "with\0nul",
            "a" * 256,
            "é" * 128,
            "\ud800",
        )
        for alias in invalid_aliases:
            with self.subTest(alias=repr(alias)):
                self.assert_invalid_snapshot(
                    value={
                        "good": {"commit": COMMIT_A},
                        alias: {"commit": COMMIT_A},
                    }
                )

    def test_aliases_at_255_utf8_bytes_are_accepted_verbatim(self):
        aliases = (
            "a" * 255,
            "é" * 127 + "a",
        )
        for alias in aliases:
            with self.subTest(alias=repr(alias)):
                self.write_config({alias: {"commit": COMMIT_A}})
                registry = self.registry()

                self.assertEqual(registry.names(), [alias])
                self.assertEqual(registry.resolve(alias)["commit"], COMMIT_A)

    def test_distinct_unicode_spellings_remain_distinct(self):
        composed = "café"
        decomposed = "cafe\u0301"
        self.write_config(
            {
                composed: {"commit": COMMIT_A},
                decomposed: {"commit": COMMIT_B},
            }
        )
        registry = self.registry()

        self.assertEqual(registry.resolve(composed)["commit"], COMMIT_A)
        self.assertEqual(registry.resolve(decomposed)["commit"], COMMIT_B)

    def test_missing_and_invalid_snapshots_are_retried(self):
        registry = self.registry()
        self.assertIsNone(registry.resolve("view"))

        self.write_raw("{")
        self.assertIsNone(registry.resolve("view"))

        self.write_config({"view": {"commit": "f" * 40}})
        self.assertIsNone(registry.resolve("view"))

        self.write_config({"view": {"commit": COMMIT_A}})
        self.assertEqual(registry.resolve("view")["commit"], COMMIT_A)

    def test_transient_commit_validation_failure_is_retried(self):
        self.write_config({"view": {"commit": COMMIT_A}})
        registry = self.registry()
        self.git.commits.remove(COMMIT_A)

        self.assertIsNone(registry.resolve("view"))
        self.git.commits.add(COMMIT_A)
        self.assertEqual(registry.resolve("view")["commit"], COMMIT_A)

    def test_successful_resolution_pins_an_immutable_complete_mapping(self):
        self.write_config({"view": {"commit": COMMIT_A}})
        registry = self.registry()

        parameters = registry.resolve("view")

        self.assertEqual(dict(parameters), {"commit": COMMIT_A})
        self.assertEqual(dict(registry.resolve("view")), dict(parameters))
        with self.assertRaises(TypeError):
            parameters["commit"] = COMMIT_B
        with self.assertRaises(TypeError):
            parameters["future"] = True

    def test_pinned_view_survives_update_removal_and_corruption(self):
        self.write_config({"view": {"commit": COMMIT_A}})
        registry = self.registry()
        pinned = registry.resolve("view")

        self.write_config({"view": {"commit": COMMIT_B}})
        self.assertEqual(dict(registry.resolve("view")), dict(pinned))

        self.write_config({"sibling": {"commit": COMMIT_B}})
        self.assertEqual(dict(registry.resolve("view")), dict(pinned))

        self.config_path.unlink()
        self.assertEqual(dict(registry.resolve("view")), dict(pinned))

        self.write_raw("{")
        self.assertEqual(dict(registry.resolve("view")), dict(pinned))
        self.assertEqual(pinned["commit"], COMMIT_A)

    def test_pinned_resolution_does_not_reopen_configuration(self):
        self.write_config({"view": {"commit": COMMIT_A}})
        registry = self.registry()
        pinned = registry.resolve("view")

        with patch(
            "git_viewspace_fs.view_config.os.open",
            side_effect=AssertionError("pinned resolution attempted configuration I/O"),
        ):
            self.assertEqual(dict(registry.resolve("view")), dict(pinned))

    def test_pin_is_per_name_not_a_cached_whole_snapshot(self):
        self.write_config(
            {
                "first": {"commit": COMMIT_A},
                "second": {"commit": COMMIT_A},
            }
        )
        registry = self.registry()
        self.assertEqual(registry.resolve("first")["commit"], COMMIT_A)

        self.write_config(
            {
                "first": {"commit": COMMIT_B},
                "second": {"commit": COMMIT_B},
            }
        )

        self.assertEqual(registry.resolve("first")["commit"], COMMIT_A)
        self.assertEqual(registry.resolve("second")["commit"], COMMIT_B)

    def test_names_does_not_pin_a_view(self):
        self.write_config({"view": {"commit": COMMIT_A}})
        registry = self.registry()

        self.assertEqual(registry.names(), ["view"])
        self.write_config({"view": {"commit": COMMIT_B}})

        self.assertEqual(registry.resolve("view")["commit"], COMMIT_B)

    def test_names_are_sorted_union_of_pinned_and_current_snapshot(self):
        self.write_config({"z-pinned": {"commit": COMMIT_A}})
        registry = self.registry()
        registry.resolve("z-pinned")

        self.write_config(
            {
                "m-current": {"commit": COMMIT_B},
                "a-current": {"commit": COMMIT_A},
            }
        )
        self.assertEqual(
            registry.names(),
            ["a-current", "m-current", "z-pinned"],
        )

        self.write_raw("{")
        self.assertEqual(registry.names(), ["z-pinned"])

        self.config_path.unlink()
        self.assertEqual(registry.names(), ["z-pinned"])

    def test_names_rereads_every_unpinned_snapshot(self):
        self.write_config({"first": {"commit": COMMIT_A}})
        registry = self.registry()

        self.assertEqual(registry.names(), ["first"])
        self.write_config({"second": {"commit": COMMIT_B}})
        self.assertEqual(registry.names(), ["second"])
        self.assertEqual(registry.resolve("second")["commit"], COMMIT_B)

    def test_lock_serializes_first_success_and_only_one_value_is_pinned(self):
        blocking_git = BlockingGitOperations()
        self.write_config({"view": {"commit": COMMIT_A}})
        registry = self.registry(git_operations=blocking_git)
        observed_lock = ObservedLock()
        registry._lock = observed_lock
        results = []
        errors = []

        def resolve_view():
            try:
                results.append(registry.resolve("view"))
            except BaseException as error:  # Preserve worker failures for main thread.
                errors.append(error)

        first = threading.Thread(target=resolve_view, daemon=True)
        second = threading.Thread(target=resolve_view, daemon=True)
        self.addCleanup(blocking_git.release.set)
        first.start()
        self.assertTrue(blocking_git.entered.wait(5))
        self.write_config({"view": {"commit": COMMIT_B}})
        second.start()
        self.assertTrue(observed_lock.second_attempt.wait(5))
        blocking_git.release.set()
        first.join(5)
        second.join(5)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(blocking_git.calls, [COMMIT_A])
        self.assertEqual(len(results), 2)
        self.assertEqual(
            [parameters["commit"] for parameters in results],
            [COMMIT_A, COMMIT_A],
        )


class ViewRegistryGitObjectTestCase(TestCase):
    @classmethod
    def setUpClass(cls):
        project_directory = Path(__file__).resolve().parents[2]
        fixture_repository = project_directory / "test_repo"
        if not (fixture_repository / ".git").is_dir():
            raise AssertionError("run git_viewspace_fs/tests/make_test_repo.sh first")

        cls._temporary_directory = tempfile.TemporaryDirectory()
        cls.directory = Path(cls._temporary_directory.name)
        cls.repository_path = cls.directory / "repo"
        shutil.copytree(fixture_repository, cls.repository_path, symlinks=True)
        cls.repository = Repository(str(cls.repository_path))
        cls.git = GitOperations(str(cls.repository_path))

        head = cls.repository.head.peel(Commit)
        cls.commit_id = str(head.id)
        cls.tree_id = str(head.tree.id)
        cls.blob_id = str(head.tree["file_a"].id)

        signature = Signature(
            "GitViewspaceFS Test",
            "git-viewspace-fs@example.invalid",
            1_234_567_890,
            0,
        )
        cls.tag_id = str(
            cls.repository.create_tag(
                "view-config-annotated-tag",
                head.id,
                ObjectType.COMMIT,
                signature,
                "tag object must not be accepted as a commit",
            )
        )
        cls.unreachable_commit_id = str(
            cls.repository.create_commit(
                None,
                signature,
                signature,
                "unreachable but valid commit",
                head.tree.id,
                [head.id],
            )
        )

    @classmethod
    def tearDownClass(cls):
        cls._temporary_directory.cleanup()

    def setUp(self):
        self.config_path = self.directory / f"views-{self._testMethodName}.json"

    def resolve_value(self, value):
        self.config_path.write_text(
            json.dumps({"view": {"commit": value}}),
            encoding="utf-8",
        )
        return ViewRegistry(str(self.config_path), self.git).resolve("view")

    def test_exact_full_commit_id_is_accepted(self):
        parameters = self.resolve_value(self.commit_id)

        self.assertEqual(parameters["commit"], self.commit_id)

    def test_ref_names_and_abbreviations_are_rejected(self):
        invalid_values = (
            "HEAD",
            "master",
            "refs/heads/master",
            "refs/tags/t20050607",
            self.commit_id[:7],
            self.commit_id[:12],
            self.commit_id.upper(),
        )
        for value in invalid_values:
            with self.subTest(value=value):
                self.assertIsNone(self.resolve_value(value))

    def test_full_blob_tree_and_tag_object_ids_are_rejected(self):
        for object_id in (self.blob_id, self.tree_id, self.tag_id):
            with self.subTest(object_id=object_id):
                self.assertIsNone(self.resolve_value(object_id))

    def test_unknown_full_object_id_is_rejected(self):
        self.assertIsNone(self.resolve_value("f" * 40))

    def test_unreachable_exact_commit_object_is_accepted(self):
        parameters = self.resolve_value(self.unreachable_commit_id)

        self.assertEqual(parameters["commit"], self.unreachable_commit_id)


if __name__ == "__main__":
    main()
