"""Command-line interface for git-viewspace."""

import argparse
import json
import sys
from pathlib import Path

from .errors import GitViewspaceError, UsageError
from .init import InitRequest, Initializer
from .service import (
    branch_merge,
    branch_pin,
    submodule_operation,
    start_background,
    start_foreground,
    status,
    stop,
    view_clean,
    view_commit,
    view_advance,
    view_deinit_overlay,
    view_destroy,
    view_init_and_start,
    view_init_overlay,
    view_init_readonly,
    view_start,
    view_routing,
    view_stage,
    view_status,
    view_stop,
    view_sync,
    view_switch,
)


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise UsageError(message)


def _view_id(value):
    if len(value) != 32 or any(character not in "0123456789abcdef" for character in value):
        raise argparse.ArgumentTypeError("must be exactly 32 lowercase hexadecimal characters")
    return value


def _add_view_id_precondition(parser):
    parser.add_argument(
        "--ensure-view-id",
        type=_view_id,
        help="require this exact existing view realization",
    )


def _view_id_keywords(arguments):
    value = getattr(arguments, "ensure_view_id", None)
    return {} if value is None else {"ensure_view_id": value}


def _parser():
    parser = _Parser(
        prog="git-viewspace",
        description="Independent agent workspaces backed by one shared Git repository.",
        epilog="Linux FUSE/OverlayFS required. Views are not security sandboxes. "
               "Use COMMAND --help for options; pass --json for machine-readable results.",
    )
    parser.add_argument("--root", required=True, help="absolute managed-root path")
    parser.add_argument("--json", action="store_true", help="emit one JSON result")
    subcommands = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)

    initialize = subcommands.add_parser("init", help="initialize a managed root")
    initialize.add_argument("--git-name", required=True, help="author/committer display name")
    initialize.add_argument("--git-email", required=True, help="author/committer email")
    sources = initialize.add_mutually_exclusive_group(required=True)
    sources.add_argument("--clone", metavar="REPOSITORY")
    sources.add_argument("--copy", metavar="REPOSITORY")
    sources.add_argument("--move", metavar="REPOSITORY")
    sources.add_argument("--empty", action="store_true")
    initialize.add_argument("--git-arg-branch", metavar="BRANCH")
    initialize.add_argument("--initial-branch", metavar="BRANCH")
    start = subcommands.add_parser("start", help="start the per-root daemon")
    start.add_argument(
        "--foreground",
        action="store_true",
        help="run attached to the invoking terminal or service supervisor",
    )
    subcommands.add_parser("status", help="report root and runtime health")
    subcommands.add_parser("stop", help="stop the daemon and all managed runtime")
    module = subcommands.add_parser("submodule", help="prepare exact code objects or commit an explicit pin")
    module_commands = module.add_subparsers(dest="module_command", required=True)
    acquire = module_commands.add_parser("prepare")
    acquire.add_argument("--at", required=True)
    acquire.add_argument("--path", required=True)
    acquire.add_argument("--commit", default="")
    update = module_commands.add_parser("pin")
    update.add_argument("--branch", required=True)
    update.add_argument("--path", required=True)
    update.add_argument("--commit", required=True)
    update.add_argument("--message", required=True)
    pin = subcommands.add_parser("branch-pin", help="pin an exact commit on a local branch")
    pin.add_argument("--branch", required=True)
    pin.add_argument("--commit", required=True)
    branch = subcommands.add_parser("branch", help="perform a managed repository branch operation")
    branch_commands = branch.add_subparsers(
        dest="branch_command",
        required=True,
        parser_class=_Parser,
    )
    merge = branch_commands.add_parser("merge", help="merge an attempt branch into its parent")
    merge.add_argument("attempt_branch")
    merge.add_argument("--into", required=True, dest="parent_branch")
    merge.add_argument("--ensure-attempt-tip", required=True)
    merge.add_argument("--message", required=True)
    merge.add_argument(
        "--checked-out-destination",
        choices=("refuse", "detach-if-clean"),
        default="refuse",
        help="refuse a checked-out destination or detach it after proving it clean",
    )
    view = subcommands.add_parser("view", help="manage one commit view")
    view_commands = view.add_subparsers(
        dest="view_command",
        required=True,
        parser_class=_Parser,
    )
    routing = view_commands.add_parser("routing", help="configure optional external-file routing on a stopped view")
    routing.add_argument("action", choices=("enable", "disable", "rebind"))
    routing.add_argument("view_name")
    routing.add_argument("--executable")
    routing.add_argument("--subtree", help="expose only this repository directory in the routed view")
    routing.add_argument("--runtime-dir", action="append", default=[], metavar="VIEW_PATH=EXTERNAL_DIRECTORY")
    _add_view_id_precondition(routing)
    view_init = view_commands.add_parser("init", help="initialize a view")
    view_init_kinds = view_init.add_subparsers(
        dest="view_kind",
        required=True,
        parser_class=_Parser,
    )
    readonly = view_init_kinds.add_parser("readonly", help="initialize a read-only view")
    readonly.add_argument("view_name")
    readonly.add_argument("--at", required=True, dest="commitish")
    overlay = view_init_kinds.add_parser("overlay", help="initialize writable storage")
    overlay.add_argument("view_name")
    overlay.add_argument("--branch", required=True)
    _add_view_id_precondition(overlay)
    init_and_start = view_commands.add_parser(
        "init-and-start",
        help="initialize and start a view in one daemon operation",
    )
    init_and_start_kinds = init_and_start.add_subparsers(
        dest="view_kind",
        required=True,
        parser_class=_Parser,
    )
    init_start_readonly = init_and_start_kinds.add_parser(
        "readonly",
        help="initialize and start a read-only view",
    )
    init_start_readonly.add_argument("view_name")
    init_start_readonly.add_argument("--at", required=True, dest="commitish")
    init_start_overlay = init_and_start_kinds.add_parser(
        "overlay",
        help="initialize writable storage and start the view",
    )
    init_start_overlay.add_argument("view_name")
    init_start_overlay.add_argument("--from-branch", required=True)
    init_start_overlay.add_argument("--new-branch", required=True)
    view_deinit = view_commands.add_parser("deinit", help="remove a view capability")
    view_deinit_kinds = view_deinit.add_subparsers(
        dest="view_kind",
        required=True,
        parser_class=_Parser,
    )
    deinit_overlay = view_deinit_kinds.add_parser("overlay", help="remove writable storage")
    deinit_overlay.add_argument("view_name")
    _add_view_id_precondition(deinit_overlay)
    for command in ("start", "stop", "sync", "stage"):
        operation = view_commands.add_parser(command, help=f"{command} a view")
        operation.add_argument("view_name")
        _add_view_id_precondition(operation)
    commit = view_commands.add_parser("commit", help="commit a staged view transition")
    commit.add_argument("view_name")
    commit.add_argument("--message", required=True)
    _add_view_id_precondition(commit)
    advance = view_commands.add_parser(
        "advance",
        help="stop, sync, stage, commit, clean, switch, and restart a writable view",
    )
    advance.add_argument("view_name")
    advance.add_argument("--message", required=True)
    _add_view_id_precondition(advance)
    view_status_parser = view_commands.add_parser("status", help="report view state")
    view_status_parser.add_argument("view_name", nargs="?")
    _add_view_id_precondition(view_status_parser)
    clean = view_commands.add_parser("clean", help="discard writable overlay changes")
    clean.add_argument("view_name")
    _add_view_id_precondition(clean)
    switch = view_commands.add_parser("switch", help="switch a stopped view")
    switch.add_argument("view_name")
    switch_target = switch.add_mutually_exclusive_group(required=True)
    switch_target.add_argument("--to", dest="commitish")
    switch_target.add_argument("--next", action="store_true", dest="use_next")
    _add_view_id_precondition(switch)
    destroy = view_commands.add_parser("destroy", help="destroy a stopped view")
    destroy.add_argument("view_name")
    destroy.add_argument("--discard", action="store_true")
    _add_view_id_precondition(destroy)
    return parser


def _request(arguments):
    if arguments.clone is not None:
        mode, source = "clone", arguments.clone
    elif arguments.copy is not None:
        mode, source = "copy", arguments.copy
    elif arguments.move is not None:
        mode, source = "move", arguments.move
    else:
        mode, source = "empty", None
    return InitRequest(
        root=Path(arguments.root),
        git_name=arguments.git_name,
        git_email=arguments.git_email,
        source_mode=mode,
        source=source,
        clone_branch=arguments.git_arg_branch,
        initial_branch=arguments.initial_branch,
    )


def _json_result(value):
    sys.stdout.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")


def _human_result(payload):
    command = payload["command"]
    if command == "init":
        state = "already initialized" if payload["idempotent"] else "initialized"
        print(f"{state}: {payload['root']}")
    elif command == "start":
        state = "already running" if payload["idempotent"] else "started"
        print(f"{state}: {payload['root']}")
    elif command == "stop":
        state = "already stopped" if payload["idempotent"] else "stopped"
        print(f"{state}: {payload['root']}")
    elif command == "status":
        daemon = "running" if payload["daemon"]["running"] else "stopped"
        viewfs = "healthy" if payload["viewfs"]["healthy"] else "stopped"
        health = "healthy" if payload["healthy"] else "attention required"
        print(f"root: {payload['root']}")
        print(f"status: {health}")
        print(f"daemon: {daemon}")
        print(f"viewfs: {viewfs}")
        print(f"repository: {payload['repository']['branch'] or '(detached)'} {payload['repository']['head']}")
        for issue in payload["issues"]:
            print(f"issue: {issue}")
    elif command == "branch merge":
        state = "already incorporated" if payload["idempotent"] else "merged"
        print(
            f"{state}: {payload['attempt_branch']}@{payload['attempt_tip']} "
            f"into {payload['parent_branch']}@{payload['parent_tip_after']}"
        )
    elif command == "branch pin":
        state = "pinned" if payload["created"] else "already pinned"
        print(f"{state}: {payload['branch']}@{payload['commit']}")
    elif command == "view status":
        for view in payload["views"]:
            print(
                f"{view['name']}: {view['actual']} "
                f"commit={view['commit']} desired_started={str(view['desired_started']).lower()}"
            )
    elif command == "view destroy":
        print(f"destroyed: {payload['name']}")
    elif command == "view sync":
        print(
            f"synced: {payload['view']['name']} "
            f"paths={len(payload['sync']['manifest'])}"
        )
    elif command == "view stage":
        print(
            f"staged: {payload['view']['name']} "
            f"tree={payload['stage']['index_tree']}"
        )
    elif command == "view commit":
        state = "committed" if payload["next"]["changed"] else "no Git change"
        print(f"{state}: {payload['view']['name']} commit={payload['next']['commit']}")
    else:
        state = "unchanged" if payload["idempotent"] else command.removeprefix("view ")
        print(f"{state}: {payload['view']['name']}")


def _command_label(argv):
    index = 0
    while index < len(argv):
        value = argv[index]
        if value == "--root":
            index += 2
            continue
        if value.startswith("--root=") or value == "--json":
            index += 1
            continue
        if value == "view":
            return "view"
        if value in ("init", "start", "status", "stop", "branch-pin", "branch"):
            return value
        return None
    return None


def run(argv=None, *, initializer=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    json_requested = "--json" in argv
    try:
        arguments = _parser().parse_args(argv)
        if arguments.command == "init":
            result = (initializer or Initializer()).initialize(_request(arguments))
            payload = {
                "ok": True,
                "command": "init",
                "root": str(result.root),
                "root_uuid": result.root_uuid,
                "repository": str(result.repository),
                "source_mode": result.source_mode,
                "idempotent": result.idempotent,
            }
        elif arguments.command == "start":
            if arguments.foreground:
                return start_foreground(arguments.root)
            payload = start_background(arguments.root)
        elif arguments.command == "status":
            payload = status(arguments.root)
        elif arguments.command == "stop":
            payload = stop(arguments.root)
        elif arguments.command == "branch-pin":
            payload = branch_pin(arguments.root, arguments.branch, arguments.commit)
        elif arguments.command == "submodule":
            keywords = {"path": arguments.path, "commit": arguments.commit}
            if arguments.module_command == "prepare":
                keywords["at"] = arguments.at
            else:
                keywords.update(branch=arguments.branch, message=arguments.message)
            payload = submodule_operation(arguments.root, arguments.module_command, **keywords)
        elif arguments.command == "branch":
            payload = branch_merge(
                arguments.root,
                arguments.attempt_branch,
                arguments.parent_branch,
                arguments.ensure_attempt_tip,
                arguments.message,
                arguments.checked_out_destination.replace("-", "_"),
            )
        elif arguments.view_command == "init":
            if arguments.view_kind == "readonly":
                payload = view_init_readonly(
                    arguments.root,
                    arguments.view_name,
                    arguments.commitish,
                )
            else:
                payload = view_init_overlay(
                    arguments.root,
                    arguments.view_name,
                    arguments.branch,
                    **_view_id_keywords(arguments),
                )
        elif arguments.view_command == "deinit":
            payload = view_deinit_overlay(
                arguments.root,
                arguments.view_name,
                **_view_id_keywords(arguments),
            )
        elif arguments.view_command == "init-and-start":
            if arguments.view_kind == "readonly":
                payload = view_init_and_start(
                    arguments.root,
                    arguments.view_name,
                    arguments.view_kind,
                    at=arguments.commitish,
                )
            else:
                payload = view_init_and_start(
                    arguments.root,
                    arguments.view_name,
                    arguments.view_kind,
                    from_branch=arguments.from_branch,
                    new_branch=arguments.new_branch,
                )
        elif arguments.view_command == "routing":
            runtime = {}
            for item in arguments.runtime_dir:
                path, separator, source = item.partition("=")
                if not separator or path in runtime:
                    raise GitViewspaceError("INVALID_REQUEST", "runtime mapping must be unique VIEW_PATH=EXTERNAL_DIRECTORY")
                runtime[path] = source
            payload = view_routing(arguments.root, arguments.view_name, arguments.action, arguments.executable, runtime_directories=runtime if arguments.runtime_dir else None, subtree=arguments.subtree, **_view_id_keywords(arguments))
        elif arguments.view_command == "start":
            payload = view_start(
                arguments.root,
                arguments.view_name,
                **_view_id_keywords(arguments),
            )
        elif arguments.view_command == "stop":
            payload = view_stop(
                arguments.root,
                arguments.view_name,
                **_view_id_keywords(arguments),
            )
        elif arguments.view_command == "sync":
            payload = view_sync(
                arguments.root,
                arguments.view_name,
                **_view_id_keywords(arguments),
            )
        elif arguments.view_command == "stage":
            payload = view_stage(
                arguments.root,
                arguments.view_name,
                **_view_id_keywords(arguments),
            )
        elif arguments.view_command == "commit":
            payload = view_commit(
                arguments.root,
                arguments.view_name,
                arguments.message,
                **_view_id_keywords(arguments),
            )
        elif arguments.view_command == "advance":
            payload = view_advance(
                arguments.root,
                arguments.view_name,
                arguments.message,
                **_view_id_keywords(arguments),
            )
        elif arguments.view_command == "status":
            if arguments.ensure_view_id is not None and arguments.view_name is None:
                raise UsageError("--ensure-view-id requires one view name")
            payload = view_status(
                arguments.root,
                arguments.view_name,
                **_view_id_keywords(arguments),
            )
        elif arguments.view_command == "switch":
            payload = view_switch(
                arguments.root,
                arguments.view_name,
                arguments.commitish,
                use_next=arguments.use_next,
                **_view_id_keywords(arguments),
            )
        elif arguments.view_command == "clean":
            payload = view_clean(
                arguments.root,
                arguments.view_name,
                **_view_id_keywords(arguments),
            )
        else:
            payload = view_destroy(
                arguments.root,
                arguments.view_name,
                arguments.discard,
                **_view_id_keywords(arguments),
            )
        if arguments.json:
            _json_result(payload)
        else:
            _human_result(payload)
        return 0
    except UsageError as error:
        if json_requested:
            _json_result(
                {
                    "ok": False,
                    "command": _command_label(argv),
                    "error": {"code": "INVALID_ARGUMENT", "message": str(error)},
                }
            )
        else:
            print(f"git-viewspace: invalid arguments: {error}", file=sys.stderr)
        return 2
    except GitViewspaceError as error:
        payload = {
            "ok": False,
            "command": _command_label(argv),
            "error": {"code": error.code, "message": error.message},
        }
        if error.detail:
            payload["error"]["detail"] = error.detail
        if error.data is not None:
            payload["error"]["data"] = error.data
        if error.partial_state is not None:
            payload["partial_state"] = error.partial_state
        if json_requested:
            _json_result(payload)
        else:
            print(f"git-viewspace: {error.message} [{error.code}]", file=sys.stderr)
            if error.detail:
                print(error.detail, file=sys.stderr)
            if error.data is not None:
                print(json.dumps(error.data, ensure_ascii=False, sort_keys=True), file=sys.stderr)
            if error.partial_state is not None:
                partial = error.partial_state
                print(
                    "partial state: "
                    f"view={partial.get('view_name')!r} "
                    f"parent_branch={partial.get('parent_branch')!r} "
                    f"branch={partial.get('branch')!r} "
                    f"branch_commit={partial.get('branch_commit')!r} "
                    f"view_created={partial.get('view_created')!r}",
                    file=sys.stderr,
                )
        return 1


def main():
    raise SystemExit(run())
