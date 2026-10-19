#!/usr/bin/env python
#
# Copyright 2017-2021 Vitalis Salis and Diomidis Spinellis
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

import argparse
import os
import re
import sys

import fuse
from git_viewspace.cache_config import content_cache_budget
from fuse import FUSE

from .filesystem import MAX_WORKLOAD_ID, GitViewspaceFS
from .instrumentation import JsonlInstrumentation

fuse.fuse_python_api = (0, 1)

_SOURCE_COMPONENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


def _at_or_below(path, directory):
    try:
        return os.path.commonpath((path, directory)) == directory
    except ValueError:
        return False


def _resolves_at_or_below(path, directory):
    return _at_or_below(path, directory) or _at_or_below(
        os.path.realpath(path),
        os.path.realpath(directory),
    )


def _workload_id(value):
    try:
        numeric_id = int(value, 10)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a non-root Linux numeric ID") from error

    if not 0 < numeric_id <= MAX_WORKLOAD_ID:
        raise argparse.ArgumentTypeError("must be a non-root Linux numeric ID")
    return numeric_id


def _source_component(value):
    if _SOURCE_COMPONENT.fullmatch(value) is None:
        raise argparse.ArgumentTypeError("must be 1-128 ASCII letters, digits, '.', '_', or '-'")
    return value


def _filesystem_source(root_uuid, attempt_token):
    return f"GitViewspaceFS:{root_uuid}:{attempt_token}"


def _process_start_time_ticks():
    """Return this process's Linux start time from ``/proc/self/stat``."""

    with open("/proc/self/stat", encoding="ascii") as input_file:
        value = input_file.read()
    _, separator, fields = value.rpartition(")")
    if not separator:
        raise RuntimeError("/proc/self/stat has no command terminator")
    # ``fields`` begins with field 3 (state); starttime is field 22.
    components = fields.split()
    if len(components) <= 19:
        raise RuntimeError("/proc/self/stat is missing the start-time field")
    start_time = int(components[19], 10)
    if start_time <= 0:
        raise RuntimeError("/proc/self/stat contains an invalid start time")
    return start_time


def _libfuse_identity():
    """Return the loaded libfuse soname and its runtime ABI version."""

    soname = os.path.basename(fuse._libfuse_path)
    version = int(fuse._libfuse.fuse_version())
    if not soname or version <= 0:
        raise RuntimeError("loaded libfuse identity is unavailable")
    return soname, version


def main(foreground=True):
    parser = argparse.ArgumentParser()
    parser.add_argument("repo", help="Git repository to expose.")
    parser.add_argument(
        "mount",
        help="Existing empty directory where the filesystem will be mounted.",
    )
    parser.add_argument(
        "--config",
        required=True,
        help="JSON file defining names under /views.",
    )
    parser.add_argument(
        "--workload-uid",
        required=True,
        type=_workload_id,
        help="Non-root numeric UID reported for every filesystem entry.",
    )
    parser.add_argument(
        "--workload-gid",
        required=True,
        type=_workload_id,
        help="Non-root numeric GID reported for every filesystem entry.",
    )
    parser.add_argument(
        "--root-uuid",
        required=True,
        type=_source_component,
        help="Managed-root UUID embedded in the filesystem source.",
    )
    parser.add_argument(
        "--attempt-token",
        required=True,
        type=_source_component,
        help="Unique process-attempt token embedded in the filesystem source.",
    )
    parser.add_argument(
        "--instrumentation-log",
        help=("Optional pre-existing workload-owned regular file for JSONL operation counters."),
    )
    parser.add_argument(
        "--content-cache-bytes",
        help="Decoded-content budget in bytes; overrides GIT_VIEWSPACE_FS_CONTENT_CACHE_BYTES. "
        "Default 268435456 (256 MiB); zero disables retention.",
    )
    args = parser.parse_args()
    try:
        cache_bytes = content_cache_budget(args.content_cache_bytes)
    except ValueError as error:
        parser.error(str(error))

    repo_path = os.path.abspath(args.repo)
    mount_path = os.path.abspath(args.mount)
    config_path = os.path.abspath(args.config)
    instrumentation_path = (
        os.path.abspath(args.instrumentation_log) if args.instrumentation_log is not None else None
    )

    if _resolves_at_or_below(config_path, mount_path):
        parser.error("--config must not be the mount directory or beneath it")
    if _resolves_at_or_below(repo_path, mount_path):
        parser.error("repository must not be the mount directory or beneath it")
    if instrumentation_path is not None:
        if _resolves_at_or_below(instrumentation_path, mount_path):
            parser.error("--instrumentation-log must not be the mount directory or beneath it")
        if _resolves_at_or_below(instrumentation_path, repo_path):
            parser.error("--instrumentation-log must not be the repository or beneath it")
        if _resolves_at_or_below(instrumentation_path, config_path):
            parser.error("--instrumentation-log must not be the configuration")

    filesystem_source = _filesystem_source(args.root_uuid, args.attempt_token)
    instrumentation = None
    try:
        if instrumentation_path is not None:
            instrumentation = JsonlInstrumentation(
                instrumentation_path,
                filesystem_source,
                args.workload_uid,
                args.workload_gid,
            )
            libfuse_soname, libfuse_api_version = _libfuse_identity()
            instrumentation.emit(
                "process_identity",
                attempt_token=args.attempt_token,
                libfuse_api_version=libfuse_api_version,
                libfuse_soname=libfuse_soname,
                pid=os.getpid(),
                proc_start_time_ticks=_process_start_time_ticks(),
                source=filesystem_source,
            )
        filesystem = GitViewspaceFS(
            repo=repo_path,
            config=config_path,
            workload_uid=args.workload_uid,
            workload_gid=args.workload_gid,
            instrumentation=instrumentation,
            content_cache_bytes=cache_bytes,
        )
        sys.stderr.write(f"Mounting repository {repo_path} at {mount_path}\n")
        FUSE(
            filesystem,
            mount_path,
            nothreads=True,
            foreground=foreground,
            fsname=filesystem_source,
            allow_other=True,
            default_permissions=True,
            negative_timeout=0,
            ro=True,
        )
    finally:
        if instrumentation is not None:
            instrumentation.close()


def console_main():
    """Run the installed command in foreground supervisor mode."""
    main(foreground=True)


if __name__ == "__main__":
    main(foreground=True)
