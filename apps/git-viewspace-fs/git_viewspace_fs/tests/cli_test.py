# SPDX-License-Identifier: Apache-2.0

import io
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from git_viewspace_fs import __main__ as cli


WORKLOAD_UID = 1234
WORKLOAD_GID = 5678
ROOT_UUID = "12345678-1234-5678-9abc-123456789abc"
ATTEMPT_TOKEN = "attempt-0001"
STARTUP_ARGUMENTS = [
    "--workload-uid",
    str(WORKLOAD_UID),
    "--workload-gid",
    str(WORKLOAD_GID),
    "--root-uuid",
    ROOT_UUID,
    "--attempt-token",
    ATTEMPT_TOKEN,
]
FILESYSTEM_SOURCE = f"GitViewspaceFS:{ROOT_UUID}:{ATTEMPT_TOKEN}"


class CLIInvocationTestCase(TestCase):
    @patch.object(cli, "FUSE")
    @patch.object(cli, "GitViewspaceFS")
    def test_module_main_runs_in_foreground_with_absolute_paths_and_mount_options(
            self, git_viewspace_fs_type, fuse):
        operations = object()
        git_viewspace_fs_type.return_value = operations

        # The configuration is deliberately absent: startup must not require a
        # readable snapshot because it may be created after the mount starts.
        argv = [
            "python -m git_viewspace_fs",
            "test_repo",
            "mnt",
            "--config",
            "missing-config.json",
            *STARTUP_ARGUMENTS,
        ]
        with patch.object(sys, "argv", argv), patch.object(cli.sys, "stderr"):
            cli.main()

        git_viewspace_fs_type.assert_called_once_with(
            repo=os.path.abspath("test_repo"),
            config=os.path.abspath("missing-config.json"),
            workload_uid=WORKLOAD_UID,
            workload_gid=WORKLOAD_GID,
            instrumentation=None,
            content_cache_bytes=268435456,
        )
        fuse.assert_called_once_with(
            operations,
            os.path.abspath("mnt"),
            nothreads=True,
            foreground=True,
            fsname=FILESYSTEM_SOURCE,
            allow_other=True,
            default_permissions=True,
            negative_timeout=0,
            ro=True,
        )

    @patch.object(cli, "FUSE")
    @patch.object(cli, "GitViewspaceFS")
    def test_existing_symlink_arguments_are_passed_as_absolute_not_real_paths(
            self, git_viewspace_fs_type, fuse):
        operations = object()
        git_viewspace_fs_type.return_value = operations

        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            repository = base / "repository"
            repository.mkdir()
            repository_link = base / "repository-link"
            repository_link.symlink_to(repository, target_is_directory=True)

            config = base / "views.json"
            config.write_text("{}", encoding="utf-8")
            config_link = base / "views-link.json"
            config_link.symlink_to(config)

            mount = base / "mount"
            mount.mkdir()
            argv = [
                "python -m git_viewspace_fs",
                str(repository_link),
                str(mount),
                "--config",
                str(config_link),
                *STARTUP_ARGUMENTS,
            ]
            with patch.object(sys, "argv", argv), \
                    patch.object(cli.sys, "stderr"):
                cli.main()

        git_viewspace_fs_type.assert_called_once_with(
            repo=os.path.abspath(repository_link),
            config=os.path.abspath(config_link),
            workload_uid=WORKLOAD_UID,
            workload_gid=WORKLOAD_GID,
            instrumentation=None,
            content_cache_bytes=268435456,
        )
        fuse.assert_called_once()

    @patch.object(cli, "FUSE")
    @patch.object(cli, "GitViewspaceFS")
    def test_optional_instrumentation_log_is_opened_and_closed_around_fuse(
            self, git_viewspace_fs_type, fuse):
        operations = object()
        git_viewspace_fs_type.return_value = operations

        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            repository = base / "repository"
            repository.mkdir()
            mount = base / "mount"
            mount.mkdir()
            config = base / "views.json"
            config.write_text("{}", encoding="utf-8")
            log_path = base / "trace.jsonl"
            log_path.touch(mode=0o600)
            startup_arguments = list(STARTUP_ARGUMENTS)
            startup_arguments[startup_arguments.index("--workload-uid") + 1] = str(os.getuid())
            startup_arguments[startup_arguments.index("--workload-gid") + 1] = str(os.getgid())
            argv = [
                "git-viewspace-fs",
                str(repository),
                str(mount),
                "--config",
                str(config),
                *startup_arguments,
                "--instrumentation-log",
                str(log_path),
            ]
            with patch.object(sys, "argv", argv), \
                    patch.object(cli.sys, "stderr"):
                cli.main()

            instrumentation = git_viewspace_fs_type.call_args.kwargs[
                "instrumentation"
            ]
            self.assertEqual(instrumentation.path, os.path.abspath(log_path))
            self.assertEqual(instrumentation.filesystem_source, FILESYSTEM_SOURCE)
            self.assertIsNone(instrumentation._descriptor)
            self.assertTrue(log_path.is_file())
            records = [
                json.loads(line)
                for line in log_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(1, len(records))
            self.assertEqual("process_identity", records[0]["event"])
            self.assertEqual(1, records[0]["event_count"])
            self.assertEqual(1, records[0]["sequence"])
            self.assertEqual(FILESYSTEM_SOURCE, records[0]["filesystem_source"])
            self.assertEqual(ATTEMPT_TOKEN, records[0]["attempt_token"])
            self.assertGreater(records[0]["libfuse_api_version"], 0)
            self.assertRegex(records[0]["libfuse_soname"], r"^libfuse\.so(?:\.[0-9]+)+$")
            self.assertEqual(os.getpid(), records[0]["pid"])
            self.assertGreater(records[0]["proc_start_time_ticks"], 0)
            self.assertEqual(FILESYSTEM_SOURCE, records[0]["source"])

        fuse.assert_called_once()

    def test_process_start_time_matches_proc_self_stat(self):
        with open("/proc/self/stat", "r", encoding="ascii") as input_file:
            value = input_file.read()
        expected = int(value.rpartition(")")[2].split()[19], 10)
        self.assertEqual(expected, cli._process_start_time_ticks())

    def test_loaded_libfuse_identity_is_reported(self):
        soname, version = cli._libfuse_identity()
        self.assertRegex(soname, r"^libfuse\.so(?:\.[0-9]+)+$")
        self.assertGreater(version, 0)

    @patch.object(cli, "FUSE")
    @patch.object(cli, "GitViewspaceFS")
    def test_instrumentation_log_cannot_modify_inputs_or_mount_namespace(
            self, git_viewspace_fs_type, fuse):
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            repository = base / "repository"
            repository.mkdir()
            mount = base / "mount"
            mount.mkdir()
            config = base / "views.json"
            config.write_text("{}", encoding="utf-8")

            for log_path in (
                mount / "trace.jsonl",
                repository / "trace.jsonl",
                config,
            ):
                argv = [
                    "git-viewspace-fs",
                    str(repository),
                    str(mount),
                    "--config",
                    str(config),
                    *STARTUP_ARGUMENTS,
                    "--instrumentation-log",
                    str(log_path),
                ]
                with self.subTest(log_path=log_path), \
                        patch.object(sys, "argv", argv), \
                        patch.object(cli.sys, "stderr", io.StringIO()), \
                        self.assertRaises(SystemExit) as raised:
                    cli.main()
                self.assertEqual(raised.exception.code, 2)

        git_viewspace_fs_type.assert_not_called()
        fuse.assert_not_called()

    @patch.object(cli, "FUSE")
    @patch.object(cli, "GitViewspaceFS")
    def test_config_is_required(self, git_viewspace_fs_type, fuse):
        argv = [
            "python -m git_viewspace_fs",
            "test_repo",
            "mnt",
            *STARTUP_ARGUMENTS,
        ]
        with patch.object(sys, "argv", argv), \
                patch.object(cli.sys, "stderr", io.StringIO()), \
                self.assertRaises(SystemExit) as raised:
            cli.main()

        self.assertEqual(raised.exception.code, 2)
        git_viewspace_fs_type.assert_not_called()
        fuse.assert_not_called()

    @patch.object(cli, "FUSE")
    @patch.object(cli, "GitViewspaceFS")
    def test_mount_identity_arguments_are_required(
            self, git_viewspace_fs_type, fuse):
        for option in (
                "--workload-uid",
                "--workload-gid",
                "--root-uuid",
                "--attempt-token"):
            startup_arguments = list(STARTUP_ARGUMENTS)
            option_index = startup_arguments.index(option)
            del startup_arguments[option_index:option_index + 2]
            argv = [
                "git-viewspace-fs",
                "test_repo",
                "mnt",
                "--config",
                "views.json",
                *startup_arguments,
            ]
            with self.subTest(option=option), \
                    patch.object(sys, "argv", argv), \
                    patch.object(cli.sys, "stderr", io.StringIO()), \
                    self.assertRaises(SystemExit) as raised:
                cli.main()
            self.assertEqual(raised.exception.code, 2)

        git_viewspace_fs_type.assert_not_called()
        fuse.assert_not_called()

    @patch.object(cli, "FUSE")
    @patch.object(cli, "GitViewspaceFS")
    def test_invalid_workload_ids_are_rejected(self, git_viewspace_fs_type, fuse):
        for option in ("--workload-uid", "--workload-gid"):
            for value in ("root", "-1", "0", str(1 << 32)):
                startup_arguments = list(STARTUP_ARGUMENTS)
                startup_arguments[startup_arguments.index(option) + 1] = value
                argv = [
                    "git-viewspace-fs",
                    "test_repo",
                    "mnt",
                    "--config",
                    "views.json",
                    *startup_arguments,
                ]
                with self.subTest(option=option, value=value), \
                        patch.object(sys, "argv", argv), \
                        patch.object(cli.sys, "stderr", io.StringIO()), \
                        self.assertRaises(SystemExit) as raised:
                    cli.main()
                self.assertEqual(raised.exception.code, 2)

        git_viewspace_fs_type.assert_not_called()
        fuse.assert_not_called()

    @patch.object(cli, "FUSE")
    @patch.object(cli, "GitViewspaceFS")
    def test_unsafe_or_ambiguous_source_components_are_rejected(
            self, git_viewspace_fs_type, fuse):
        for option in ("--root-uuid", "--attempt-token"):
            invalid_values = (
                "has space",
                "has,comma",
                "has:colon",
                "has/slash",
                "a" * 129,
            )
            for value in invalid_values:
                startup_arguments = list(STARTUP_ARGUMENTS)
                startup_arguments[startup_arguments.index(option) + 1] = value
                argv = [
                    "git-viewspace-fs",
                    "test_repo",
                    "mnt",
                    "--config",
                    "views.json",
                    *startup_arguments,
                ]
                with self.subTest(option=option, value=value), \
                        patch.object(sys, "argv", argv), \
                        patch.object(cli.sys, "stderr", io.StringIO()), \
                        self.assertRaises(SystemExit) as raised:
                    cli.main()
                self.assertEqual(raised.exception.code, 2)

        git_viewspace_fs_type.assert_not_called()
        fuse.assert_not_called()

    @patch.object(cli, "FUSE")
    @patch.object(cli, "GitViewspaceFS")
    def test_obsolete_namespace_and_cache_options_are_rejected(
            self, git_viewspace_fs_type, fuse):
        for obsolete_option in (
                "--hash-trees", "--no-ref-symlinks", "--no-cache"):
            with self.subTest(option=obsolete_option):
                argv = [
                    "git-viewspace-fs",
                    "test_repo",
                    "mnt",
                    "--config",
                    "views.json",
                    *STARTUP_ARGUMENTS,
                    obsolete_option,
                ]
                with patch.object(sys, "argv", argv), \
                        patch.object(cli.sys, "stderr", io.StringIO()), \
                        self.assertRaises(SystemExit) as raised:
                    cli.main()
                self.assertEqual(raised.exception.code, 2)

        git_viewspace_fs_type.assert_not_called()
        fuse.assert_not_called()

    @patch.object(cli, "FUSE")
    @patch.object(cli, "GitViewspaceFS")
    def test_lexical_repository_and_config_paths_at_or_below_mount_are_rejected(
            self, git_viewspace_fs_type, fuse):
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            mount = base / "mount"
            mount.mkdir()
            outside_repository = base / "repository"
            outside_config = base / "views.json"

            cases = (
                (mount, outside_config),
                (mount / "repository", outside_config),
                (outside_repository, mount),
                (outside_repository, mount / "views.json"),
            )
            for repository, config in cases:
                with self.subTest(repository=repository, config=config):
                    argv = [
                        "git-viewspace-fs",
                        str(repository),
                        str(mount),
                        "--config",
                        str(config),
                        *STARTUP_ARGUMENTS,
                    ]
                    with patch.object(sys, "argv", argv), \
                            patch.object(cli.sys, "stderr", io.StringIO()), \
                            self.assertRaises(SystemExit) as raised:
                        cli.main()
                    self.assertEqual(raised.exception.code, 2)

        git_viewspace_fs_type.assert_not_called()
        fuse.assert_not_called()

    @patch.object(cli, "FUSE")
    @patch.object(cli, "GitViewspaceFS")
    def test_real_repository_and_config_paths_below_mount_are_rejected(
            self, git_viewspace_fs_type, fuse):
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            mount = base / "mount"
            mount.mkdir()
            repository_in_mount = mount / "repository"
            repository_in_mount.mkdir()
            config_in_mount = mount / "views.json"
            config_in_mount.write_text("{}", encoding="utf-8")

            repository_link = base / "repository-link"
            repository_link.symlink_to(
                repository_in_mount,
                target_is_directory=True,
            )
            config_link = base / "views-link.json"
            config_link.symlink_to(config_in_mount)
            mount_link = base / "mount-link"
            mount_link.symlink_to(mount, target_is_directory=True)

            cases = (
                (repository_link, mount, base / "outside.json"),
                (base / "outside-repository", mount, config_link),
                (repository_in_mount, mount_link, base / "outside.json"),
                (base / "outside-repository", mount_link, config_in_mount),
            )
            for repository, requested_mount, config in cases:
                with self.subTest(
                        repository=repository,
                        mount=requested_mount,
                        config=config):
                    argv = [
                        "git-viewspace-fs",
                        str(repository),
                        str(requested_mount),
                        "--config",
                        str(config),
                        *STARTUP_ARGUMENTS,
                    ]
                    with patch.object(sys, "argv", argv), \
                            patch.object(cli.sys, "stderr", io.StringIO()), \
                            self.assertRaises(SystemExit) as raised:
                        cli.main()
                    self.assertEqual(raised.exception.code, 2)

        git_viewspace_fs_type.assert_not_called()
        fuse.assert_not_called()

    @patch.object(cli, "FUSE")
    @patch.object(cli, "GitViewspaceFS")
    def test_sibling_paths_that_only_share_the_mount_prefix_are_allowed(
            self, git_viewspace_fs_type, fuse):
        operations = object()
        git_viewspace_fs_type.return_value = operations

        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            mount = base / "mount"
            mount.mkdir()
            repository = base / "mount-repository"
            repository.mkdir()
            config = base / "mount-views.json"
            config.write_text("{}", encoding="utf-8")

            argv = [
                "git-viewspace-fs",
                str(repository),
                str(mount),
                "--config",
                str(config),
                *STARTUP_ARGUMENTS,
            ]
            with patch.object(sys, "argv", argv), \
                    patch.object(cli.sys, "stderr"):
                cli.main()

        git_viewspace_fs_type.assert_called_once_with(
            repo=os.path.abspath(repository),
            config=os.path.abspath(config),
            workload_uid=WORKLOAD_UID,
            workload_gid=WORKLOAD_GID,
            instrumentation=None,
            content_cache_bytes=268435456,
        )
        fuse.assert_called_once_with(
            operations,
            os.path.abspath(mount),
            nothreads=True,
            foreground=True,
            fsname=FILESYSTEM_SOURCE,
            allow_other=True,
            default_permissions=True,
            negative_timeout=0,
            ro=True,
        )

    @patch.object(cli, "main")
    def test_console_command_stays_in_foreground_for_supervision(self, main):
        cli.console_main()

        main.assert_called_once_with(foreground=True)
