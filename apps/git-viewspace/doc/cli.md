# CLI reference

Every command uses the same general form:

```text
git-viewspace --root <absolute-root> [--json] <command> [arguments]
```

`--root` is always required. The initializing non-root UID and primary GID own
the root and must run every later command. `--json` returns one JSON object;
without it the CLI prints a concise summary. Exit status is `0` for success,
`1` for an operational failure, and `2` for invalid command syntax.

Except for `init`, `start`, `status`, and `stop`, commands are serialized
requests to the running daemon.

Commands that act on an existing named view accept the optional precondition
`--ensure-view-id=<view-id>`. The ID is exactly 32 lowercase hexadecimal
characters. On the online path, the daemon first reconciles durable state with
its in-memory state and then rejects a different current realization with
`VIEW_ID_MISMATCH`, before the requested view operation has any side effect.
Offline named `view status` performs the equivalent comparison against its
validated durable record. Omitting the option preserves ordinary name-based
CLI behavior. It applies to `view init overlay`, `start`, `stop`, named
`status`, `sync`, `stage`, `commit`,
`advance`, `clean`, `switch`, `deinit overlay`, and `destroy`. It does not apply
to root commands, list-all `view status`, or view-creation commands.

## Root commands

### `init`

```text
git-viewspace --root <root> init \
  --git-name <name> --git-email <email> \
  (--clone <repository> [--git-arg-branch <branch>] |
   --copy <repository> |
   --move <repository> |
   --empty [--initial-branch <branch>])
```

Initializes the root and publishes its sole managed checkout at `<root>/repo`.
All source modes produce the same validated, local, non-bare SHA-1 repository
contract.

- `--clone` lets ordinary Git use the caller's existing credentials. The only
  reviewed clone option is `--git-arg-branch`.
- `--copy` preserves the source repository.
- `--move` consumes the source using a same-filesystem rename.
- `--empty` creates one empty root commit; its default branch is `main`.
- `--git-name` and `--git-email` become the controlled identity for later
  managed commits.

Initialization is retryable and idempotent for the same arguments. It rejects
dirty, shallow, sparse, bare, linked-worktree, or otherwise unsupported repositories.
Top-level pinned submodules have a restricted explicit workflow; see
[submodule support](../../../SUBMODULES.md).

### `start`

```text
git-viewspace --root <root> start [--foreground]
```

Validates the root, acquires its locks, reconciles recognized runtime state,
starts git-viewspace-fs, restores views whose desired state is started, and publishes
the control socket. `--foreground` keeps the daemon attached to a terminal or
service supervisor. Starting an already healthy daemon is idempotent.

### `status`

```text
git-viewspace --root <root> status
```

Reports repository, daemon, git-viewspace-fs, mount, view, and pending-transition
state. When the daemon is stopped, status performs read-only offline
inspection; it does not repair state.

### `stop`

```text
git-viewspace --root <root> stop
```

Stops command admission, removes every public view mount, unmounts git-viewspace-fs,
terminates manager-owned children, and removes runtime state. It is the
forceful whole-root cleanup boundary. View definitions and their
`desired_started` values remain durable for the next daemon start.

## Repository operations

### `submodule prepare` and `submodule pin`

```text
git-viewspace --root <root> submodule prepare --at <commit-or-ref> \
  --path <registered-path> [--commit <exact-module-commit>]
git-viewspace --root <root> submodule pin --branch <branch> \
  --path <registered-path> --commit <exact-module-commit> --message <message>
```

`prepare` explicitly acquires the registered module's pinned commit, or the
requested replacement commit, into the root's module object store. `pin`
commits a prepared replacement gitlink on an unclaimed, non-checked-out branch.
Neither operation changes existing views. See [submodules](../../../SUBMODULES.md)
for URL restrictions, deadlines, failure handling, and view lifecycle.

### `branch-pin`

```text
git-viewspace --root <root> branch-pin \
  --branch <branch> \
  --commit <commit>
```

Creates an absent direct local branch at one exact commit under the running daemon's repository
lock. `--commit` must be exactly 40 lowercase hexadecimal characters and identify a commit object;
symbolic names, abbreviations, uppercase IDs, and tag, tree, or blob objects are invalid. The
commit need not be reachable from another ref, so a still-resolvable dangling commit is accepted.

Creation compare-and-updates the ref against absence. An identical existing direct branch is
idempotent success with `created: false`, including when another identical creator won a race. The
command never moves an existing branch: another tip returns `BRANCH_DIVERGED`, with branch,
expected commit, and actual commit in JSON error data. A symbolic ref returns
`SYMBOLIC_BRANCH_UNSUPPORTED`; malformed commits return `INVALID_COMMIT`, missing commit objects
return `COMMIT_NOT_FOUND`, and invalid branch names return `INVALID_BRANCH`.

Successful JSON contains exactly the established root identity plus `command: "branch pin"`, the
requested `branch` and `commit`, and Boolean `created`. The operation creates or changes no view,
mount, index, working tree, checkout attachment, or other ref. It retains the pinned branch and
provides no deletion or garbage-collection action.

### `branch merge`

```text
git-viewspace --root <root> branch merge <attempt-branch> \
  --into <parent-branch> \
  --ensure-attempt-tip <commit> \
  --message <message> \
  [--checked-out-destination <refuse|detach-if-clean>]
```

Verifies the exact direct-local attempt tip, resolves the current direct-local parent tip,
computes a two-parent merge commit without checking out either branch, and
atomically advances only the parent ref. The attempt ref and every view remain
unchanged. A retry returns success without another commit when the exact
attempt tip is already an ancestor of the parent.

A writable view claim on the parent returns `BRANCH_CLAIMED`. The default
`refuse` policy rejects a parent attached to the managed checkout.
`detach-if-clean` instead requires that checkout at the exact parent tip with
a clean index and worktree, including no untracked paths. After computing the
merge and creating its commit object, it detaches HEAD at the same tip without
rewriting the index or worktree, then compare-and-updates the parent ref. A
failed publication may therefore leave a clean detached checkout at the old
tip, which a retry can reuse.

Content conflicts are detected before any detachment and return
`BRANCH_MERGE_CONFLICT`; JSON error data contains at most 100 paths and 64 KiB
in `conflict_paths_rr`, plus the total conflict count and a truncation flag.
Missing attempt and parent branches use `ATTEMPT_BRANCH_NOT_FOUND` and
`PARENT_BRANCH_NOT_FOUND`. `ATTEMPT_TIP_MISMATCH` and `PARENT_TIP_MOVED`
retain expected and actual commit identities in `error.data`. The command
never stops, starts, cleans, advances, or destroys a view and never deletes
either branch. System/global Git configuration and custom repository merge
drivers are not part of the merge policy.

## View definition and mount commands

### `view init readonly`

```text
git-viewspace --root <root> view init readonly <view> --at <commit-ish>
```

Creates a stopped view pinned to one resolved commit and publishes an immutable
git-viewspace-fs alias. It does not create a public mount.

### `view init overlay`

```text
git-viewspace --root <root> view init overlay <view> --branch <branch> [--ensure-view-id=<view-id>]
```

Upgrades an existing stopped read-only definition to writable state. The
branch must exist as a direct local ref, be unclaimed, and point to the pinned commit. The command
creates a fresh upper/work generation but does not mount it.

### `view init-and-start`

```text
git-viewspace --root <root> view init-and-start readonly <view> --at <commit-ish>
git-viewspace --root <root> view init-and-start overlay <view> \
  --from-branch <parent-branch> --new-branch <attempt-branch>
```

Runs initialization and start as one daemon request, avoiding repeated client
startup and intermediate status work. The overlay form resolves the exact tip
of a direct local `--from-branch`, creates `--new-branch` there, and refuses any direct or
symbolic existing attempt ref as `BRANCH_EXISTS` before creating a view. On
success `branch_creation` reports both names and the exact shared fork commit.
A later compound-step failure reports `VIEW_INIT_PARTIAL` plus the retained
branch identity and never deletes that branch. This is not an idempotent attach
operation; use standalone overlay initialization only to upgrade a stopped
read-only view onto an existing compatible branch.

### `view start` and `view stop`

```text
git-viewspace --root <root> view start <view> [--ensure-view-id=<view-id>]
git-viewspace --root <root> view stop <view> [--ensure-view-id=<view-id>]
```

`start` publishes either a read-only bind mount or a writable OverlayFS mount
at `<root>/views/<view>/mnt`. `stop` performs an orderly unmount and preserves
the definition, branch claim, generation, and pending Git records. Both are
idempotent in an already consistent state.

### `view status`

```text
git-viewspace --root <root> view status
git-viewspace --root <root> view status <view> [--ensure-view-id=<view-id>]
```

Reports all views or one selected view. Writable reports include branch,
base, generation, dirty state, and any durable sync, stage, or next result.

## Writable Git transition commands

These commands require a stopped writable view.

### `view sync`

```text
git-viewspace --root <root> view sync <view> [--ensure-view-id=<view-id>]
```

Resets the singular checkout to the view's owned branch/base, validates the
complete stopped upper, replays it directly into the checkout as the ordinary
workload UID/GID, and records the upper fingerprint and literal Git path
manifest. It does not stage or commit.

### `view stage`

```text
git-viewspace --root <root> view stage <view> [--ensure-view-id=<view-id>]
```

Resets the real index to the sync base and stages the recorded manifest using
literal, NUL-delimited `git add -A --force` semantics. It verifies that no
changed or untracked path remains and records the resulting index tree.

See [Behavior and limitations](behavior.md#gitignore-and-unconditional-staging)
before relying on ordinary `.gitignore` behavior.

### `view commit`

```text
git-viewspace --root <root> view commit <view> --message <message> [--ensure-view-id=<view-id>]
```

Commits the validated index using the root's configured Git identity, with the
recorded base as its sole parent. Hooks and commit signing are disabled. If the
staged tree equals the base tree, no empty commit is created. Either outcome is
recorded as a pending `next` transition.

### `view advance`

```text
git-viewspace --root <root> view advance <view> --message <message> [--ensure-view-id=<view-id>]
```

Performs this sequence in one daemon request:

```text
stop -> sync -> stage -> commit -> clean -> switch --next -> start
```

The input view may be started or stopped. The command resumes from durable
sync, stage, or next state after an interrupted attempt. It avoids duplicate
between-command validation while preserving each durable recovery boundary.

## Reset, switch, and removal

### `view clean`

```text
git-viewspace --root <root> view clean <view> [--ensure-view-id=<view-id>]
```

For a writable view, resets the managed checkout, discards the current
upper/work generation, and creates a fresh empty generation. A committed
`next` result is preserved so it can be consumed by `switch --next`.

### `view switch`

```text
git-viewspace --root <root> view switch <view> --to <commit-ish> [--ensure-view-id=<view-id>]
git-viewspace --root <root> view switch <view> --next [--ensure-view-id=<view-id>]
```

Switches a clean, stopped view to another commit and publishes a fresh
immutable alias. Writable views may switch only to their owned branch tip.
`--next` consumes the pending result produced by `view commit`.

### `view deinit overlay`

```text
git-viewspace --root <root> view deinit overlay <view> [--ensure-view-id=<view-id>]
```

Removes clean, stopped writable storage and releases its branch claim while
preserving the read-only view definition.

### `view destroy`

```text
git-viewspace --root <root> view destroy <view> [--discard] [--ensure-view-id=<view-id>]
```

Destroys a stopped view. Dirty writable state or a pending transition is
rejected unless `--discard` explicitly authorizes its loss. The common
existing-view identity precondition applies. Successful JSON output includes
the destroyed `view_id`.

## Optional routed external files

`view routing enable NAME --executable ABSOLUTE_HELPER [--runtime-dir RELATIVE=ABSOLUTE]...`
configures a stopped view. `view routing disable NAME` removes that configuration;
`view routing rebind NAME` clears prior admissions; subsequent access lazily admits external identities.
The optional `--ensure-view-id ID` guard prevents targeting a recreated view.
See [routing](routing.md) for descriptor format, installation, supported operations,
readonly behavior and recovery constraints.

Routing can optionally select `--subtree REPOSITORY_PATH`. The public mount exposes
that directory's contents, while advance still commits the underlying full-tree
overlay. Runtime paths are relative to the selected root. See [routing.md](routing.md).
