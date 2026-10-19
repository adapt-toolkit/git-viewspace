# `git-viewspace` Command-Line Specification

Status: normative CLI specification

Implementation status: Stages 0–8, the `view init-and-start` and `view advance`
compositions, attempt-branch creation, and the repository-only `branch merge` and
`branch-pin` primitives are implemented. The private daemon protocol is version 4. Stage 8b added
packaging, installed-artifact acceptance, and documentation without defining additional MVP view
semantics.

Related architecture: [`git-viewspace` architecture](git-viewspace-architecture.md)

Current pinned submodule support is documented in [SUBMODULES.md](../../../SUBMODULES.md).

This document defines the command-line and state-transition contract for
`git-viewspace`. The related architecture document defines directory layout,
durability, locking, mount profiles, OverlayFS interpretation, recovery, and
safety requirements.

The initial CLI deliberately exposes small foundational operations. Later
compound commands may compose these primitives, but must reuse their exact
validation and transition implementations.

## 1. Process model

One initialized root manages exactly one Git repository. Each running root has:

- one `git-viewspace` daemon holding the root and repository locks;
- one supervised git-viewspace-fs child shared by all views of that repository;
- zero or more manager-owned read-only or writable public view mounts; and
- one serialized mutation stream.

Different roots and repositories may have independent daemons running at the
same time. Within one root, the daemon is the sole manager of its checkout,
view state, git-viewspace-fs configuration, overlay generations, and mounts.

The daemon exists to own and recover runtime state, supervise git-viewspace-fs,
restore desired mounts, and define a complete cleanup boundary. It is not
intended to provide concurrent mutation execution.

## 2. General command form

```text
git-viewspace --root <absolute-root> [--json] <command> [arguments]
```

Rules:

- `--root` is required for every command and is resolved to one canonical
  absolute path.
- No command accepts an alternate repository, checkout, mountpoint, upperdir,
  workdir, state path, socket, or git-viewspace-fs mount path. Those paths are
  derived from the initialized root.
- `--json` selects one stable JSON result object on standard output. Without
  it, the command emits concise human-readable output.
- Diagnostics go to standard error.
- Manager Git operations use controlled policy. The one exception is
  `init --clone`, which intentionally lets ordinary `git clone` use the
  caller's existing Git/SSH authentication configuration and environment.
- Mutating commands other than `init` and `start` are serialized requests to
  the running daemon. A client never starts a hidden daemon or git-viewspace-fs
  process.
- The private daemon protocol is strict version 4. Version 4 adds bounded mutation queue budgets
  and qualified snapshot status. Version 3 is rejected, not inferred or upgraded. The socket
  frontend remains responsive while one worker serializes mutations and state observations.
- Read-only status commands use the daemon while it is running and may perform
  locked, read-only offline inspection otherwise.

Exit status:

- `0`: the requested state or idempotent equivalent was established;
- `1`: a validated operation failed, including busy, divergence, recovery, or
  consistency failures; and
- `2`: invalid command syntax or an invalid caller-supplied argument.

Operation failures have stable symbolic error codes in JSON output. A failed
command never reports success merely because partial cleanup was attempted.

## 3. Shared identities and names

### 3.1 Manager and workload identity

Initialization records the caller's numeric UID and primary GID as both the
manager owner and workload identity. The caller must be non-root, and its real
and effective UID and GID must match. `git-viewspace` does not infer an identity from
`sudo` environment variables and does not accept an identity override.

Every later command and daemon startup for the root requires the same recorded
UID/GID. Privileged mount, unmount, inspection, and replay helpers receive that
recorded identity explicitly and return to it before performing ordinary-user
filesystem changes.

The workload is expected to use the repository only through public views;
direct access to the managed checkout or manager storage is unsupported, but
the MVP does not require OS-level isolation from the same local user.

### 3.2 View names

A view name is one validated UTF-8 filename component. It must not:

- be empty, `.`, or `..`;
- be `.git`;
- contain `/` or NUL; or
- violate the manager's reserved-name or filesystem-component policy.

A view name is never interpreted as a path.

### 3.3 View realization identity

Every view definition has an opaque manager-generated `view_id`. Commands that
act on an existing named realization optionally accept
`--ensure-view-id=<view-id>`, where the value is exactly 32 lowercase
hexadecimal characters. On the online path, after reconciling durable state
with daemon memory and before dispatching the operation, the daemon returns
`VIEW_ID_MISMATCH` if the name currently denotes a different realization. The
rejected operation has no side effect. Offline named `view status` makes the
same comparison against its validated durable view record.

This precondition is available on `view init overlay`, `start`, `stop`, named
`status`, `sync`, `stage`, `commit`, `advance`, `clean`, `switch`, `deinit
overlay`, and `destroy`. It is optional for direct human CLI use. A persistent
typed handle supplies its last observed ID on every existing-view mutation.
Explicit handle refresh remains an unguarded observation because its purpose is
to discover and, under caller policy, adopt the realization currently bound to
the logical name.

The precondition is not accepted by root commands, list-all `view status`,
`view init readonly`, or `view init-and-start`. Those view commands create or
retry creation rather than address a caller-pinned existing realization.
Successful responses for surviving views include the current `view_id` in the
view report; `view destroy` instead returns the destroyed `view_id` directly.

### 3.4 Commit arguments

A caller-supplied commit-ish is resolved once, under the controlled Git
environment, to one immutable commit OID. Durable state records the OID, not
the caller's spelling.

## 4. Root lifecycle

### 4.1 `init`

```text
git-viewspace --root <root> init \
  --git-name <name> \
  --git-email <email> \
  <source-mode>
```

Exactly one source mode is required:

```text
--clone <repository> [--git-arg-branch <branch>]
--copy <existing-repository>
--move <existing-repository>
--empty [--initial-branch <branch>]
```

Common rules:

- `init` is the only offline mutation.
- `init` rejects UID 0, setuid/setgid execution, and any mismatch
  between the caller and an existing root's recorded owner identity.
- No daemon may own the target root or source repository.
- The managed repository is always `<root>/repo`.
- Initialization is durable, retryable, and idempotent for the exact same
  completed root or recognized initialization transaction.
- A completed result is a local, non-bare, SHA-1 Git repository with one
  ordinary checkout.
- The checkout must be clean, have no unresolved index entries, and have no
  merge, rebase, cherry-pick, revert, bisect, or similar operation in progress.
- The repository must contain at least one commit.
- The root, source, repository, and manager identity must pass the topology,
  ownership, symlink, filesystem, and repository-claim checks from the MVP
  architecture.
- `--git-name` and `--git-email` are stored as manager configuration for later
  commits. Later commands do not inherit identity from ambient Git config.

#### `--clone`

`--clone` takes exactly one repository URL or local repository path. `git-viewspace`
always supplies the destination and exposes no general Git-option passthrough.
The sole reviewed clone option is `--git-arg-branch <branch>`, which maps to
Git's `--branch <branch>` and is valid only with `--clone`. Its position
relative to `--clone` is immaterial. The value must be a valid branch name,
and the completed clone must have that branch checked out rather than a tag or
detached commit.

For example:

```text
git-viewspace --root <root> init <common-options> \
  --clone https://example.invalid/project.git \
  --git-arg-branch main
```

The effective invocation is equivalent to:

```text
git clone [--branch <branch>] -- <repository> <transaction-repository>
```

The transaction repository is validated and then published as `<root>/repo`.
The repository argument is passed to Git unchanged. If it embeds credentials,
their exposure or persistence is the caller's responsibility. Existing Git
credential helpers, SSH configuration, agents, and authentication environment
apply normally; `git-viewspace` adds no credential mechanism.

If Git clone fails, `git-viewspace` reports Git's diagnostic and the stable
`GIT_CLONE_FAILED` error code, cleans or retains only its recognized retryable
transaction state, and advises the caller to configure Git authentication or
prepare a clean local checkout and retry with `--move`. The manager validates
every successful clone rather than trusting Git's exit status alone.

#### `--copy`

`--copy <existing-repository>` validates a local initialized repository and
copies the complete repository directory into `<root>/repo`. The source is
unchanged. The manager validates the copied result independently before
publishing initialization success.

#### `--move`

`--move <existing-repository>` performs the same validation as `--copy`, then
moves the complete repository directory into `<root>/repo`. The initial
implementation requires a same-filesystem rename. It does not silently degrade
to copy-and-delete.

#### `--empty`

`--empty` creates a new repository and one empty root commit using the
configured Git identity. The default initial branch is `main`:

```text
git-viewspace --root <root> init <common-options> --empty
git-viewspace --root <root> init <common-options> \
  --empty --initial-branch <branch>
```

`--initial-branch` is valid only with `--empty`.

### 4.2 `start`

```text
git-viewspace --root <root> start [--foreground]
```

`start`:

1. validates the initialized root and repository;
2. acquires permanent daemon-lifetime root and repository locks;
3. reconciles durable state and manager-owned runtime residue;
4. reconstructs and publishes the exact git-viewspace-fs configuration;
5. launches and verifies one git-viewspace-fs child;
6. restores every view whose durable desired state is started; and
7. reports success only after the daemon is ready to serve commands.

Without `--foreground`, the command launches the daemon and waits for its ready
result before returning. With `--foreground`, the daemon remains attached to
the invoking terminal or service supervisor.

If the exact daemon is already healthy, `start` succeeds idempotently. It
rejects a second or inconsistent owner.

### 4.3 `status`

```text
git-viewspace --root <root> status
```

Reports at least:

- initialization, schema, root identity, and repository health;
- checkout branch, HEAD, object format, cleanliness, and in-progress Git state;
- daemon, locks, socket, git-viewspace-fs child, and private mount health;
- every view's definition and desired/actual mount state; and
- pending transactions, recovery requirements, foreign residue, or
  inconsistencies.

When the daemon is not running, `status` is read-only and does not repair or
adopt state.

### 4.4 `stop`

```text
git-viewspace --root <root> stop
```

Whole-daemon stop is intentionally forceful and has no additional `--force`
flag. It:

1. stops admitting new mutations and drains any already admitted operation;
2. removes every manager-owned public view mount, writable or read-only;
3. removes the private git-viewspace-fs mount;
4. terminates git-viewspace-fs and every other manager-owned subprocess;
5. retains each view's durable desired-started state for later recovery;
6. removes its socket and runtime-owned residue; and
7. releases locks and exits.

The daemon attempts an orderly unmount first and forcefully detaches a managed
mount when needed to make shutdown complete. It does not kill unrelated
workload processes. Success means that no manager-owned mount remains visible
in the managed namespace and no manager-owned child remains alive. If that
cannot be established, `stop` returns failure with the remaining state.

Stopping an already stopped root is idempotent after read-only verification
that no manager-owned runtime state remains.

### 4.5 Online validation boundary

Full repository qualification—including the checkout ownership walk, Git
configuration checks, commit-tree inspection, and connectivity check—runs at
initialization and daemon startup/recovery. It does not run before each online
view command.

An online client validates the immutable managed-root identity, manager-state
schema, requested view's durable state record, runtime process identity, and
control-socket identity, then sends the requested operation directly. Before
executing it, the daemon compares the relevant durable manager/view records
and git-viewspace-fs configuration with its authoritative in-memory state. It then
checks any optional existing-view identity precondition before command
dispatch. Commands perform only their state-machine, mount, upper-delta, and
Git checks after those admission checks succeed.

Offline `status`, offline `stop`, daemon startup, and explicit recovery retain
the deeper filesystem and repository validation because no running daemon is
available to own and attest the state.

## 5. View state model

A durable view has a read-only definition containing:

- its validated view name;
- one pinned immutable commit OID;
- one immutable git-viewspace-fs alias; and
- its desired-started state.

A view may additionally have writable state containing:

- exclusive ownership of one existing branch;
- one overlay base commit, which initially equals the pinned commit;
- one active marked upper/work generation; and
- optional current-generation sync, stage, and next-commit records.

Runtime state is separate from both definitions. A view may be:

- stopped;
- started read-only, using a public read-only mount of its git-viewspace-fs alias; or
- started writable, using OverlayFS over that alias and its active generation.

The following implications always hold:

- writable initialization requires read-only initialization;
- started state requires read-only initialization;
- a started view may be read-only or writable;
- sync, stage, commit, clean, switch, overlay deinitialization, and destruction
  require the view to be stopped unless their command states otherwise; and
- one branch may be owned by at most one view in one managed repository.

## 6. View initialization

### 6.1 `view init readonly`

```text
git-viewspace --root <root> view init readonly <view> --at <commit-ish>
```

Requires a running daemon. It:

- validates that `<view>` is unused;
- resolves `--at` to a representable immutable commit;
- creates the durable read-only view definition;
- allocates and publishes a unique immutable git-viewspace-fs alias; and
- leaves the view stopped and undesired.

It does not create a public mount, claim a branch, or create upper/work
storage. Repeating the exact initialization is idempotent; conflicting
arguments fail.

### 6.2 `view init overlay`

```text
git-viewspace --root <root> view init overlay <view> --branch <branch> [--ensure-view-id=<view-id>]
```

Requires:

- an existing read-only definition;
- a stopped, undesired view;
- no existing overlay definition;
- an existing direct local branch not owned by another view; and
- a branch tip exactly equal to the view's pinned commit.

It claims the branch exclusively, creates a fresh marked upper/work
generation, records the pinned commit as its overlay base, and leaves the view
stopped. It does not mount the view.

### 6.3 `view init-and-start`

```text
git-viewspace --root <root> view init-and-start readonly <view> --at <commit-ish>
git-viewspace --root <root> view init-and-start overlay <view> \
  --from-branch <parent-branch> --new-branch <attempt-branch>
```

This is one daemon request composed from the same initialization and mount
primitives. The read-only form performs `init readonly` then `start`. The
overlay form resolves the exact direct-local parent tip, creates `<attempt-branch>` there, then
performs `init readonly`, `init overlay`, and `start`. The branch must not
already exist in direct or symbolic form; creation uses a compare-against-absent ref update after
an explicit symbolic-ref check, and a collision is a pre-effect `BRANCH_EXISTS` refusal with no
view. Success reports both branch names and the exact fork commit. Failures after
the branch is created return `VIEW_INIT_PARTIAL` with branch/view residue for inspection and never
delete the branch. This compound
overlay form is deliberately not idempotent; `view init overlay` remains the
separate existing-branch operation used during recovery.

## 7. View mount lifecycle

### 7.1 `view start`

```text
git-viewspace --root <root> view start <view> [--ensure-view-id=<view-id>]
```

Requires read-only initialization but not overlay initialization.

- For a read-only-only view, it creates the public read-only mount from the
  pinned git-viewspace-fs alias.
- For a writable view, it validates branch tip equals overlay base, validates
  the active generation and fixed mount profile, and mounts OverlayFS.
- It durably records `desired_started=true` only through the defined mount
  transaction.
- It succeeds idempotently if the exact expected mount is already healthy.
- It refuses conflicting mounts, unexplained residue, alias mismatch, or
  writable branch/base divergence.

### 7.2 `view stop`

```text
git-viewspace --root <root> view stop <view> [--ensure-view-id=<view-id>]
```

Unmounts the public mount whether it is read-only or writable and durably sets
`desired_started=false`. It preserves the view definition, branch claim,
overlay generation, and pending Git state.

Unlike whole-daemon `stop`, `view stop` uses an orderly unmount and returns a
structured busy failure rather than force-detaching the individual view. It is
idempotent when the view is already consistently stopped and undesired.

### 7.3 `view status`

```text
git-viewspace --root <root> view status
git-viewspace --root <root> view status <view> [--ensure-view-id=<view-id>]
```

Without a name, it lists all views. With a name, it reports one view in detail.
The report includes:

- pinned commit and immutable alias;
- optional owned branch and its current tip;
- optional overlay base and active generation;
- desired and actual mount kind/state;
- upperdir dirtiness;
- sync-manifest, staged-tree, and next-commit state; and
- compatibility, divergence, pending transaction, and consistency status.

Like root `status`, it may perform read-only offline inspection when no daemon
owns the root.

## 8. Writable and repository Git primitives

### 8.1 `view sync`

```text
git-viewspace --root <root> view sync <view> [--ensure-view-id=<view-id>]
```

Requires:

- an initialized writable overlay;
- a stopped and undesired view;
- branch tip exactly equal to overlay base;
- a validated stopped upper/work generation; and
- no unresolved recovery state.

It:

1. resets the singular shared checkout and index to the overlay base;
2. validates and replays the stopped upperdir into the checkout;
3. emits and durably records the exact literal Git path manifest required for
   targeted staging; and
4. records the view, generation, base, upper fingerprint, and manifest identity
   needed to reject stale follow-up operations.

It does not stage or commit. Repeating `sync` starts from the recorded base and
must reproduce the same synchronized checkout and manifest for an unchanged
generation.

### 8.2 `view stage`

```text
git-viewspace --root <root> view stage <view> [--ensure-view-id=<view-id>]
```

Requires a successful, still-current `view sync`. It rejects a changed branch,
base, generation, checkout, index, manifest, or repository operation state.

It stages exactly the recorded paths using literal, NUL-delimited targeted
`git add -A --force` semantics. It then verifies that no changed or untracked
Git-relevant path was omitted, records the resulting index tree, and leaves the
checkout staged but uncommitted.

An empty manifest is valid and performs no `git add` invocation.

### 8.3 `view commit`

```text
git-viewspace --root <root> view commit <view> --message <message> [--ensure-view-id=<view-id>]
```

Requires a successful, still-current `view stage` and a nonempty commit
message. It revalidates the branch, expected parent, checkout, index tree,
generation, sync, and stage identities before committing.

If the staged tree differs from the parent tree, it creates one commit using
the configured manager identity and advances the owned branch. The new commit
must have the expected prior branch tip as its sole parent.

If the staged tree equals the parent tree, it creates no empty commit and
records a Git no-op result.

In either case it durably records a `next` transition result for later
`view switch --next`. This primitive does not clear the upperdir, rotate the
overlay, switch the pinned alias, or restart the view.

### 8.4 `view advance`

```text
git-viewspace --root <root> view advance <view> --message <message> [--ensure-view-id=<view-id>]
```

For a writable view, this one daemon request performs:

```text
view stop
view sync
view stage
view commit
view clean
view switch --next
view start
```

The initial stop is idempotent, so the command accepts either a started or a
stopped view. It resumes from an already durable sync, stage, or next record
instead of discarding completed work. The daemon validates durable state
against memory once at request admission; the constituent operations preserve
their state-machine, mount, upper-artifact, Git parent/index/tree, and final
mount checks without repeating equivalent upper scans, temporary-index tree
construction, intermediate status queries, or the low-level verbs' defensive
between-command checkout scans. The exact synchronized tree, staged index,
commit parent/tree, and final mount remain verified. The result includes
per-step durations for diagnostics as well as the final started view state.

### 8.5 `branch merge`

```text
git-viewspace --root <root> branch merge <attempt-branch> \
  --into <parent-branch> \
  --ensure-attempt-tip <commit> \
  --message <message> \
  [--checked-out-destination <refuse|detach-if-clean>]
```

This is a repository operation, not a view transition. It validates both direct local
branch names and the exact lowercase SHA-1 attempt tip, resolves the current
parent tip, computes a merge tree directly from the two commits, creates an
explicit commit with parent order `(parent, attempt)`, and compare-and-updates
only `refs/heads/<parent-branch>`. The attempt ref, view definitions, overlays,
and mounts are not changed.

The parent must not be claimed by a writable view. The default `refuse` policy
also rejects the manager's current checkout. Under `detach-if-clean`, the
operation first computes the merge tree and commit, then requires the attached
checkout to be at the exact parent tip with a clean index and worktree,
including untracked state. It detaches HEAD at that same commit without
rewriting the index or worktree, verifies the result, and compare-and-updates
the parent ref. A checkout already detached or attached to another branch is
not changed. A clean retry is idempotent when the exact attempt commit is
already an ancestor of the current parent and performs no detachment.

Merge conflicts are computed before detachment, return
`BRANCH_MERGE_CONFLICT`, and leave both refs and checkout attachment unchanged.
Structured error data reports `conflict_path_count`, at most 100 UTF-8
repository-relative paths totaling at most 64 KiB in `conflict_paths_rr`, and
`conflict_paths_truncated`. Missing attempt and parent branches are distinct
typed errors. Attempt-tip mismatch and parent-tip movement retain their
branch, expected tip, and actual tip. A failure after safe detachment can leave
the destination ref unchanged and the checkout cleanly detached at its old
tip; retry uses the ordinary ancestor/CAS rules. The command creates no
conflict-resolution workspace, durable transaction, automatic rollback, or
branch-deletion path.

### 8.6 `branch-pin`

```text
git-viewspace --root <root> branch-pin \
  --branch <branch> \
  --commit <commit>
```

This repository operation requires the running daemon. It accepts one valid local branch name and
one exact 40-character lowercase SHA-1 object ID whose object is itself a commit. Symbolic names
such as `HEAD`, abbreviated or uppercase IDs, and tag, tree, or blob object IDs are invalid. No
reachability from another ref is required, so a still-resolvable dangling commit is valid.

The protocol-4 request contains the following fields, plus an optional mutation
`queue_timeout_seconds` budget (see [daemon operation](../doc/daemon.md)):

```json
{"protocol":4,"command":"branch-pin","branch":"<branch>","commit":"<commit>"}
```

Under the repository lock, the daemon verifies that `refs/heads/<branch>` is not symbolic and
compare-and-updates it from the all-zero absent value to `<commit>` without dereferencing it. An
absent branch reports `created: true`. A concurrent or earlier identical direct branch is
idempotent success with `created: false`; an existing tip is never moved.

The success result contains these operation fields plus control-loop timing metadata:

```json
{"ok":true,"command":"branch pin","root":"<root>","root_uuid":"<root-uuid>","branch":"<branch>","commit":"<commit>","created":true}
```

Command-specific refusals are `INVALID_BRANCH`; `INVALID_COMMIT` for malformed text or a
non-commit object; `COMMIT_NOT_FOUND`; `SYMBOLIC_BRANCH_UNSUPPORTED`; `BRANCH_DIVERGED`, whose JSON
error data retains `branch`, `expected_commit`, and `actual_commit`; and `GIT_FAILED` for another
unclassifiable inspection or ref-update failure. Validated input and existing-ref refusals happen
before a ref change. A client that loses the response after dispatch cannot infer whether creation
completed; retrying the identical branch and commit safely converges on idempotent success.

`branch-pin` creates or mutates no view, alias, branch claim, overlay, mount, index, working tree,
checkout attachment, or other ref. A successful branch is retained independently of every view.
There is no deletion or garbage-collection operation, and the manager does not police manual ref
edits outside the pin call.

## 9. View reset and switch primitives

### 9.1 `view clean`

```text
git-viewspace --root <root> view clean <view> [--ensure-view-id=<view-id>]
```

Requires a stopped and undesired view.

For a writable view, it:

- discards the current upper/work generation;
- creates a fresh empty marked generation against the current pinned base;
- resets the shared checkout and index to the committed `next` tree when one
  exists, otherwise to the current pinned base; and
- invalidates sync and stage records tied to the discarded generation.

For a read-only-only view, it is an idempotent no-op after state validation.

A successfully committed `next` result is preserved so that `view switch
--next` remains possible. The explicit verb is the caller's authorization to
discard uncommitted overlay changes; there is no redundant `--force` flag.

### 9.2 `view switch`

```text
git-viewspace --root <root> view switch <view> --to <commit-ish> [--ensure-view-id=<view-id>]
git-viewspace --root <root> view switch <view> --next [--ensure-view-id=<view-id>]
```

Exactly one of `--to` and `--next` is required. The view must be stopped,
undesired, and clean, with no current sync or stage record.

- `--to` resolves the supplied commit-ish once.
- `--next` consumes the result recorded by the latest successful `view
  commit`. A Git no-op resolves to the unchanged commit.
- A read-only-only view may switch to any representable commit.
- A writable view may switch only to the current tip of its owned branch.

The command publishes a fresh immutable alias and then atomically updates the
pinned commit. A writable view receives a fresh empty marked overlay generation
against the new base; an upperdir is never reused across different lower
commits. A consumed `next` record is cleared. The view remains stopped and
undesired.

## 10. View deinitialization and destruction

### 10.1 `view deinit overlay`

```text
git-viewspace --root <root> view deinit overlay <view> [--ensure-view-id=<view-id>]
```

Removes writable capability while preserving the read-only view definition.
It requires the view to be stopped, undesired, clean, and free of sync, stage,
or next-commit state. It removes the marked overlay generation and writable
state, then releases the branch claim. The pinned commit and alias remain.

It is idempotent for an existing read-only-only view.

### 10.2 `view destroy`

```text
git-viewspace --root <root> view destroy <view>
git-viewspace --root <root> view destroy <view> --discard
git-viewspace --root <root> view destroy <view> --ensure-view-id=<view-id>
```

Requires a stopped and undesired view.

The common existing-view identity precondition from section 3.3 applies.
Successful JSON output always identifies the destroyed view with `view_id`.

Without `--discard`, it refuses a dirty overlay, synchronized or staged
checkout state, a pending next transition, or any unexplained view artifact.
With `--discard`, those manager-owned view-local states are explicitly
discarded after exact ownership and mount checks.

The command removes the exact manager-owned view tree and aliases, releases an
owned branch, and removes the durable view definition. It never removes the
Git branch itself, repository, shared views directory, or any path outside the
validated view tree.

## 11. State and command validation matrix

| Command | Daemon | Read-only definition | Overlay | Mount state | Additional requirement |
|---|---:|---:|---:|---|---|
| `init` | stopped | n/a | n/a | no managed mounts | new/retryable root and one source mode |
| `start` | stopped or healthy | any | any | recoverable | initialized consistent root |
| `status` | either | any | any | any | read-only inspection |
| `stop` | running or absent | any | any | any managed state | forceful manager-owned cleanup |
| `branch-pin` | running | any | any | unchanged | exact commit; direct branch absent or already at that commit |
| `branch merge` | running | any | any | unchanged or clean detachment | exact attempt tip; unclaimed parent; explicit checked-out-destination policy |
| `view init readonly` | running | absent | absent | stopped | representable commit |
| `view init overlay` | running | present | absent | stopped/undesired | unowned branch tip equals pinned commit |
| `view init-and-start readonly` | running | absent or matching | absent | stopped or exact started state | representable commit |
| `view init-and-start overlay` | running | absent | absent | stopped | parent exists; attempt ref absent; new branch created at resolved parent tip |
| `view start` | running | present | optional | stopped or exact started state | writable branch tip equals base |
| `view stop` | running | present | optional | started or exact stopped state | orderly unmount |
| `view status` | either | optional | optional | any | read-only inspection |
| `view sync` | running | present | present | stopped/undesired | branch tip equals overlay base |
| `view stage` | running | present | present | stopped/undesired | current sync record |
| `view commit` | running | present | present | stopped/undesired | current stage record and message |
| `view advance` | running | present | present | either | writable transition state and message |
| `view clean` | running | present | optional | stopped/undesired | explicit discard authorization |
| `view switch` | running | present | optional | stopped/undesired and clean | exactly one of `--to`/`--next` |
| `view deinit overlay` | running | present | optional | stopped/undesired and clean | no sync/stage/next state |
| `view destroy` | running | present | optional | stopped/undesired | clean unless `--discard` |

## 12. Primitive lifecycle examples

### 12.1 Read-only view

```text
git-viewspace --root <root> view init readonly docs --at <commit>
git-viewspace --root <root> view start docs
# workload reads the public view
git-viewspace --root <root> view stop docs
git-viewspace --root <root> view switch docs --to <other-commit>
git-viewspace --root <root> view start docs
```

### 12.2 Writable commit transition

```text
git-viewspace --root <root> view init readonly work --at <branch-tip>
git-viewspace --root <root> view init overlay work --branch <branch>
git-viewspace --root <root> view start work
# workload changes files
git-viewspace --root <root> view stop work
git-viewspace --root <root> view sync work
git-viewspace --root <root> view stage work
git-viewspace --root <root> view commit work --message "describe the change"
git-viewspace --root <root> view clean work
git-viewspace --root <root> view switch work --next
git-viewspace --root <root> view start work
```

The same lifecycle may be issued as one checked daemon operation:

```text
git-viewspace --root <root> view advance work --message "describe the change"
```

## 13. Commands intentionally omitted from the initial CLI

- `view list`: `view status` without a view name provides the list.
- `recover`: daemon `start` performs recovery before readiness.
- `restart`: may later compose root or view stop/start.
- `unstage`: `view clean` invalidates synchronized/staged state and resets the
  checkout as part of its explicit destructive operation.

## 14. Implementation stage sequence

Development proceeds as vertical CLI slices. Each stage leaves additional
working `git-viewspace` commands tested end to end. Internal libraries and system
boundaries grow only as the command under construction requires them; they are
not implemented as speculative horizontal subsystems.

### Stage 0: Retained feasibility baseline

Status: **implemented and retained**.

Stage 0 is complete and remains the evidence base for later implementation.
It retains:

- qualified git-viewspace-fs, FUSE, ext4, and OverlayFS behavior;
- the OverlayFS artifact-format specification;
- the OverlaySync implementation and 50 ABC scenarios;
- the Git commit, checkout deletion, restoration, and comparison suite; and
- the performance benchmark, ViewFS cache, and recorded findings.

No production manager implementation belongs in this stage.

Exit gate: the retained evidence, specifications, fixtures, and tests remain
reproducible and usable by later stages.

### Stage 1: Python foundation and `init`

Status: **implemented**.

Build the Python `git-viewspace` package and:

```text
git-viewspace --root <root> init ...
```

This stage implements:

- CLI parsing and human/JSON output;
- the minimal internal path, filesystem, and process boundary needed by init;
- the managed-root layout and versioned schemas;
- atomic durable JSON persistence;
- repository ownership claims and stable lock files;
- workload and Git identity validation;
- retryable initialization transactions; and
- `--clone`, `--copy`, `--move`, and `--empty` source modes.

The internal system boundary is extended in later stages before a command uses
a new host operation. Stage 1 does not attempt to predict every future system
interface.

Exit gate: every source mode produces the same valid managed-root contract, and
initialization is retryable and idempotent at every publication boundary.

### Stage 2: Daemon lifecycle

Status: **implemented**.

Add:

```text
git-viewspace --root <root> start [--foreground]
git-viewspace --root <root> status
git-viewspace --root <root> stop
```

This stage implements:

- one daemon per root and repository;
- the private Unix socket and synchronous client protocol;
- serialized mutations;
- daemon-lifetime root and repository locks;
- git-viewspace-fs configuration generation;
- git-viewspace-fs launch, readiness, supervision, and recovery;
- online and read-only offline root status;
- empty-root runtime reconciliation; and
- complete shutdown of mounts and manager-owned children.

Exit gate: an initialized empty root repeatedly starts and stops cleanly,
recovers its git-viewspace-fs child after failure, and leaves no managed mounts or
processes after successful shutdown.

### Stage 3: Read-only view lifecycle

Status: **implemented**.

Add:

```text
git-viewspace view init readonly <view> --at <commit>
git-viewspace view start <view>
git-viewspace view stop <view>
git-viewspace view status [<view>]
git-viewspace view switch <view> --to <commit>
git-viewspace view destroy <view> [--discard]
```

This stage implements durable read-only definitions, immutable aliases, public
read-only mounts, desired-versus-actual state, daemon-restart recovery,
read-only switching, destruction, and multiple same- or different-commit
views.

Exit gate: multiple read-only views can be initialized, mounted, stopped,
switched, recovered, and destroyed without affecting one another.

### Stage 4: Writable overlay lifecycle

Status: **implemented**.

Add:

```text
git-viewspace view init overlay <view> --branch <branch>
git-viewspace view deinit overlay <view>
git-viewspace view clean <view>
```

Extend `view start`, `view stop`, `view status`, and `view destroy` to writable
views. This stage implements exclusive branch claims, strict branch/base
equality, versioned upper/work generations, writable OverlayFS lifecycle,
dirty-state classification, clean generation replacement, conversion back to
read-only state, and writable-view recovery.

Exit gate: an ordinary workload can modify persistent writable views, stop and
remount without loss, clean them, deinitialize writable storage, and recover
them across daemon and git-viewspace-fs restarts.

### Stage 5: `view sync`

Status: **implemented**.

Add:

```text
git-viewspace view sync <view>
```

This stage integrates the qualified OverlaySync work into the daemon:

- stopped-view and branch/base validation;
- complete privileged upperdir inventory;
- irreversible privilege drop before checkout mutation;
- direct replay into the singular checkout;
- whiteout, opacity, redirect, type-replacement, and literal-name handling;
- a durable generation-bound Git path manifest;
- repeatable reset and resync; and
- complete upper validation before direct replay mutation. The manager-owned
  checkout may first be reset to the recorded base so a failed retry still
  leaves it in the one allowed clean state.

Exit gate: every supported scenario produces the independent model Git tree,
every unsupported artifact fails before replay, and the manifest describes the
complete Git delta.

### Stage 6: `view stage`

Status: **implemented**.

Add:

```text
git-viewspace view stage <view>
```

This stage implements current-sync validation, literal NUL-delimited targeted
`git add -A --force`, empty-manifest handling, verification that no changed
path was omitted, a durable staged-tree identity, and rejection of stale
branch, checkout, index, generation, or manifest state. The stopped upper was
already authoritatively inventoried by `view sync`; staging validates the
durable sync record and verifies its checkout/index result rather than scanning
the upper a second time. Sync does not construct a temporary index; the real
index is built and verified once here.

Exit gate: targeted staging produces the same index tree as an independent
full staging operation across the complete scenario suite.

### Stage 7: `view commit`, clean transition, and `switch --next`

Status: **implemented**.

Add:

```text
git-viewspace view commit <view> --message <message>
git-viewspace view switch <view> --next
```

Complete the writable transition semantics:

- commit the validated staged index with the expected sole parent;
- use the controlled manager Git identity;
- handle a Git no-op without creating an empty commit;
- durably record a `next` transition;
- preserve `next` through `view clean`;
- switch to the committed branch tip;
- publish a fresh immutable alias and empty overlay generation; and
- clear the consumed transition.

The complete primitive sequence is:

```text
view stop
view sync
view stage
view commit
view clean
view switch --next
view start
```

Exit gate: changed and no-op transitions both complete correctly, including
retry at each fully published `sync`, `stage`, and `next` boundary.

### Stage 8: Bounded hardening, then packaging

Status: **implemented**. Core daemon recovery, child supervision, forceful
shutdown, stable JSON responses, mount-enabled lifecycle tests, packaging, and
user documentation are present. The work was deliberately divided into two
stages with different change policies.

#### Stage 8a: Conservative implementation hardening

Status: **implemented**.

Stage 8a audits the implemented CLI, daemon, state transitions, subprocess
supervision, mount cleanup, OverlaySync boundary, and existing tests. It may
make small, localized code changes when they have a clear correctness or
operability benefit and negligible steady-state performance cost. Typical
work includes tightening an existing validation, improving a diagnostic,
closing a small cleanup hole, or adding a focused regression test.

Stage 8a does **not** presume complete recovery from every possible crash or
host failure. The following require explicit design consultation before they
are implemented:

- substantial new recovery machinery or broad transaction frameworks;
- major blocks of code interspersed through the working implementation;
- changes that add repository-wide walks or materially slow normal commands;
- new concurrency, hostile-local-user, or distributed-coordination defenses;
  and
- behavioral changes that expand or narrow the supported MVP contract.

When a failure mode would require disproportionate code, complexity, or
runtime cost, Stage 8a records the limitation and the operator-visible recovery
procedure instead of silently expanding the implementation. Existing cheap
state-machine, daemon-identity, view-identity, and mount-state checks remain;
hardening must not reintroduce expensive repository-wide validation into the
normal lifecycle.

Stage 8a evaluates both sides of every proposed fix: the concrete failure it
prevents and the code, maintenance, recovery, and performance cost it adds.
Major changes and any meaningful performance compromise are presented for
approval before implementation.

Exit gate: the selected low-cost fixes and focused regression tests pass; the
implemented recovery boundary and known unrecoverable cases are stated
truthfully; and no unapproved major mechanism or normal-path performance
regression has been introduced.

#### Stage 8b: Packaging and documentation

Status: **implemented**.

Stage 8b adds no new runtime semantics. Packaging metadata and a packaging-only
helper relocation or lookup-path change are permitted when needed to install
the implementation established at the end of Stage 8a. The stage packages and
explains that implementation, including:

- rudimentary installation and fresh-host prerequisite guidance;
- distribution of the OverlaySync helper with the installed `git-viewspace`
  package, plus an installed-package sync acceptance check;
- service-manager examples and shutdown expectations;
- CLI, daemon, state, recovery, and limitation documentation;
- a supported-host acceptance checklist;
- release-oriented test commands and known environmental requirements; and
- consistency cleanup across specifications and operator documentation.

If Stage 8b discovers a code defect, it records and routes that defect back to
a separately approved implementation change rather than fixing it implicitly
as packaging work.

Exit gate: the existing implementation can be installed, exercised, and
operated from the documentation on a supported host, and the documentation
does not promise recovery or behavior beyond what Stage 8a established.

The installed manager and git-viewspace-fs distributions passed the bounded
artifact-installed daemon, read-only-view, writable sync/stage/commit, restart,
and cleanup acceptance lifecycle. The manager wheel contains the authoritative
OverlaySync implementation beside the other `git_viewspace` modules.

Convenience commands remain thin compositions over the proven primitive
implementations rather than alternate transition paths. `view init-and-start`
and `view advance` are the initial two such compositions; a general restart
command remains deferred.
