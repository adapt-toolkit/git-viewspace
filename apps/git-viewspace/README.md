# git-viewspace

See [submodule support](../../SUBMODULES.md) for exact pinned submodules.
That contract supersedes submodule exclusions in historical specifications.

`git-viewspace` manages persistent read-only and writable views of immutable Git
commits. The current implementation provides initialization, the per-root
daemon lifecycle, persistent read-only views, and writable OverlayFS views.
Stopped writable views can be synchronized into the singular managed Git
checkout, staged through their exact sync manifest, committed, cleaned, and
switched to the resulting commit. The repository-only `branch merge` command
can integrate one exact attempt tip into a parent without changing either
branch's views or deleting the attempt branch. Its explicit checked-out-destination
policy can safely detach an exact clean parent checkout before advancing that parent.
The repository-only `branch-pin` command can retain one exact commit on an absent
direct local branch without creating or changing a view.

Status probes do not queue behind publication: they return the last consistent observation,
its age, and current operation/phase and queue occupancy. They do not claim to inspect
half-published state. The client keeps a 30-second total connection/response safety limit.
`DAEMON_TIMEOUT`, `DAEMON_BUSY` and `DAEMON_UNREACHABLE` do not
establish daemon absence and never justify offline inspection or mutation replay. Inspect
public status before retrying an operation with an uncertain outcome. Offline status uses
the existing root ownership lock. Successful `view advance` already returns its commit.

The protocol is now strict version 4: upgrade clients and daemon together during a coordinated
shutdown/restart. One worker still serializes Git, mounts, observation and supervision; there
are no parallel index mutations. Admission is bounded to 128 peers / 64 queued mutations, with
five-second incomplete-input/reply-delivery limits and explicit undispatched refusals.
Running Git is not forcibly cancelled. See [control semantics](doc/daemon.md#responsive-status-and-bounded-admission).

The private ViewFS cache defaults to 256 MiB of retained decoded content across views.
Set `GIT_VIEWSPACE_FS_CONTENT_CACHE_BYTES` before daemon startup to override it (`0` disables);
the value is forwarded explicitly through sudo and remains fixed for that daemon lifetime.
This limits retained payload, not total memory or transient oversized reads.

Practical documentation:

- [CLI reference](doc/cli.md)
- [Daemon and recovery](doc/daemon.md)
- [Behavior and limitations](doc/behavior.md)
- [Examples](doc/examples.md)
- [Installation and host prerequisites](doc/installation.md)
- [Service operation](doc/service.md)
- [Operations and troubleshooting](doc/operations.md)
- [Installed-artifact acceptance](doc/acceptance.md)

Stages 0–8, compound writable creation/advance, attempt-branch creation, and the repository-only
merge and exact branch-pin primitives are complete. The root `git-viewspace` 0.1.0
distribution includes the manager, filesystem backend, and OverlaySync helper.

Install the local package and initialize one managed root:

```sh
python3 -m pip install .

git-viewspace --root /absolute/path/to/root init \
  --git-name "VIEWSPACE User" \
  --git-email user@example.invalid \
  --clone https://example.invalid/project.git
```

Source modes are `--clone`, `--copy`, `--move`, and `--empty`.
`--git-arg-branch` is the sole optional Git clone argument. Authentication is
ordinary Git authentication: git-viewspace adds no credential subsystem. On clone
failure, prepare a clean local checkout and initialize it with `--move`.

The filesystem backend is installed automatically. Start, inspect, and stop the
per-root daemon:

```sh
git-viewspace --root /absolute/path/to/root start
git-viewspace --root /absolute/path/to/root status
git-viewspace --root /absolute/path/to/root stop
```

`start --foreground` keeps the daemon attached to a service supervisor or
terminal. The daemon owns the root and repository locks, maintains one private
read-only git-viewspace-fs mount, restarts git-viewspace-fs after failure, and removes its
mount and child on `stop`. Starting or stopping an already established state is
idempotent. Mount-enabled validation is available as
`tests/run-stage2-host.py`; it keeps its disposable root under a caller-supplied
scratch directory.

Create and operate a read-only view while the daemon is running:

```sh
git-viewspace --root /absolute/path/to/root view init readonly docs --at HEAD
git-viewspace --root /absolute/path/to/root view start docs
git-viewspace --root /absolute/path/to/root view status docs
git-viewspace --root /absolute/path/to/root view stop docs
git-viewspace --root /absolute/path/to/root view switch docs --to HEAD~1
git-viewspace --root /absolute/path/to/root view destroy docs
```

Or initialize and start it in one daemon request:

```sh
git-viewspace --root /absolute/path/to/root view init-and-start readonly docs --at HEAD
```

The public path is `<root>/views/<view>/mnt`. It is a read-only bind mount of
the view's private immutable git-viewspace-fs alias. Whole-daemon stop preserves the
desired-started state, and the next daemon start restores those public mounts.
The bounded mount-enabled Stage 3 check is `tests/run-stage3-host.py`.

Upgrade a stopped read-only view to writable storage owned by one matching
local branch:

```sh
git-viewspace --root /absolute/path/to/root view init overlay work --branch main
git-viewspace --root /absolute/path/to/root view start work
# ordinary file changes happen under root/views/work/mnt
git-viewspace --root /absolute/path/to/root view stop work
git-viewspace --root /absolute/path/to/root view status work
git-viewspace --root /absolute/path/to/root view sync work
git-viewspace --root /absolute/path/to/root view stage work
git-viewspace --root /absolute/path/to/root view commit work --message "describe the change"
git-viewspace --root /absolute/path/to/root view clean work
git-viewspace --root /absolute/path/to/root view switch work --next
git-viewspace --root /absolute/path/to/root view deinit overlay work
```

The one-request forms for creation and a complete writable transition are:

```sh
git-viewspace --root /absolute/path/to/root view init-and-start overlay work \
  --from-branch main --new-branch runner-work
# ordinary file changes happen under root/views/work/mnt
git-viewspace --root /absolute/path/to/root view advance work \
  --message "describe the change"
```

After the attempt reaches its terminal commit, integrate it into its parent
without changing the attempt view:

```sh
git-viewspace --root /absolute/path/to/root branch merge runner-work \
  --into main \
  --ensure-attempt-tip 0123456789012345678901234567890123456789 \
  --message "Merge runner-work" \
  --checked-out-destination detach-if-clean
```

The parent must not be claimed by a writable view. By default, the manager also
refuses a parent attached to its repository checkout. `detach-if-clean` permits
that case only at the exact parent tip with a clean index and worktree; it detaches
HEAD without rewriting checkout contents before advancing the parent ref. The
source branch and view remain unchanged and restartable. Conflicts are computed
before detachment, leave both refs unchanged, and report bounded
repository-relative paths as `conflict_paths_rr`; a retry after an
already-published merge is idempotent.

Pin a publication source or other immutable repository input without a view:

```sh
git-viewspace --root /absolute/path/to/root branch-pin \
  --branch viewspace/publications/runner-id/000042 \
  --commit 0123456789012345678901234567890123456789
```

The commit argument must be the exact lowercase 40-character SHA-1 ID of a commit object; names,
abbreviations, uppercase IDs, and tag objects are rejected. The command atomically creates only an
absent direct branch. Repeating the same branch and commit succeeds with `created: false`, while a
different existing tip or a symbolic ref is refused without changing it. No reachability check is
made, so a still-resolvable dangling commit can be pinned. The ref is retained: `branch-pin` does
not create a view, alter a checkout, or provide deletion or garbage collection.

Each writable view has one branch claim and one versioned upper/work
generation permanently bound to its pinned commit. `clean` discards the
stopped upper by publishing a fresh empty generation. Whole-daemon recovery
restores desired writable mounts with the same generation.

`view sync` requires a stopped writable view whose owned branch still equals
its overlay base. It validates the managed upper root, resets the singular
checkout to that base, and invokes one authoritative OverlaySync inventory and
direct replay pass. Replay irreversibly becomes the recorded
ordinary workload UID/GID before writing checkout paths. The command verifies
and durably binds the inventory-derived manifest, upper fingerprint,
generation, base, and branch. Repeating sync resets and reproduces the same
result. `view stage` validates that durable sync and checkout delta, then uses
its literal, NUL-delimited manifest to stage the synchronized tree. It verifies
that no changed or untracked path was omitted, and its durable stage record
binds the sync identity to the real Git index tree. `view commit`
validates the durable stage/index state and commits with the configured manager identity and
expected sole parent. An unchanged tree creates no empty commit. In both cases
the command records a durable `next` transition.

`view clean` resets the checkout, clears generation-bound sync and stage
records, and rotates the overlay while preserving `next`. `view switch
--next` consumes that record, publishes a fresh immutable alias and writable
generation at the branch tip, and leaves the view stopped. The intermediate
`sync`, `stage`, and `next` records are shown by online and offline `view
status`.

Online commands validate root, runtime/socket, and the relevant durable
state-machine identity. The daemon matches those records against its in-memory
state once per request. Whole-repository ownership, tree, attribute, and
connectivity qualification is reserved for initialization, daemon
startup/recovery, and offline diagnosis rather than repeated before every view
verb.

The bounded mount-enabled checks are `tests/run-stage4-host.py` and
`tests/run-stage5-host.py`, and `tests/run-stage7-host.py`. The latter covers
changed and Git-no-op commit transitions, including durable daemon restart
boundaries. The 50-case replay corpus remains under
`OverlaySync/tests/cases`.

The normative command and storage contracts are in
[`spec/git-viewspace-specification.md`](spec/git-viewspace-specification.md) and
[`spec/git-viewspace-architecture.md`](spec/git-viewspace-architecture.md).
