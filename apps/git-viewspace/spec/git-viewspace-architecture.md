# `git-viewspace` Architecture Specification

Status: normative MVP architecture

Implementation status: the process, storage, mount, view, and Git-transition
architecture in Sections 1–18 is implemented. Section 19 is covered by unit,
mount-enabled, replay-corpus, benchmark, and installed-artifact acceptance
evidence.

Command contract and implementation sequence:
[`git-viewspace-specification.md`](git-viewspace-specification.md)

Qualified OverlayFS input format:
[`OverlayFS-artifact-format.md`](OverlayFS-artifact-format.md)

This document defines the durable storage, ownership, runtime, recovery, and
integration rules beneath the `git-viewspace` CLI. It is intentionally
language-neutral. The MVP implementation is Python, but on-disk formats and
process boundaries must not depend on Python object serialization.

## 1. Objective

`git-viewspace` provides persistent filesystem views of immutable Git commits:

- git-viewspace-fs lazily exposes commit trees through one read-only FUSE mount per
  managed repository.
- A view may expose an immutable commit read-only or add a persistent ext4
  OverlayFS upper/work generation for ordinary-user writes.
- One daemon owns and recovers the repository's runtime state, supervises
  git-viewspace-fs, manages all public mounts, and cleans them up on shutdown.
- Explicit stopped-view commands replay OverlayFS state into the repository's
  singular checkout, stage a validated manifest, commit, clean, and switch the
  view to a new immutable base.

The manager interprets Git trees and qualified OverlayFS artifacts. It is not
a general filesystem snapshotter.

## 2. MVP limits

The MVP deliberately supports:

- Linux, FUSE, OverlayFS, and local ext4 upper/work storage;
- ordinary, complete, non-bare SHA-1 Git repositories;
- one checkout and one daemon per managed root;
- one non-root manager/workload UID/GID, captured from the initializing caller,
  per root;
- UTF-8 Git paths and symlink targets;
- regular files, executable-class regular files, symlinks, and Git trees; and
- serialized manager mutations.

It does not support:

- implicit network Git operations on filesystem reads;
- shallow or sparse repositories, linked worktrees, gitfile worktrees,
  unsupported submodule registrations, replace objects, or alternate object stores;
- repositories externally mutated after initialization;
- hard-linked upper entries, special files, invalid UTF-8, or unknown
  OverlayFS metadata;
- arbitrary working-tree transformations such as filters,
  `working-tree-encoding`, ident expansion, line-ending normalization, or
  custom merge drivers;
- several manager processes sharing one root or checkout; or
- transparent cross-repository coordination.

Different roots may run independent daemons concurrently.

Top-level pinned submodules and explicit post-initialization acquisition are
supported as described in [SUBMODULES.md](../../../SUBMODULES.md).

## 3. Process and ownership model

One running root contains:

```text
one git-viewspace daemon --root <root>
├── one supervised git-viewspace-fs child
├── one private local control socket
├── zero or more public read-only view mounts
└── zero or more public writable OverlayFS mounts
```

The daemon:

- holds the root and repository locks for its lifetime;
- is the sole writer of manager configuration and state;
- serializes all mutations;
- owns mount creation, recovery, and removal;
- supervises and recovers git-viewspace-fs; and
- removes every manager-owned mount and child on successful shutdown.

The initializing caller, daemon, CLI clients, and ordinary view workload share
one recorded non-root UID and primary GID. The MVP accepts no configured user
override. Privileged helpers are narrow subprocess boundaries for mount,
unmount, stopped-upper inspection, and replay preparation; ordinary checkout
mutation executes as the recorded identity.

The workload may concurrently read and write a started public view. The caller
must quiesce relevant workload activity before an orderly `view stop`. The
manager does not inspect or kill unrelated workload processes.

## 4. Managed-root layout

```text
<root>/
├── repo/                         sole Git checkout, including repo/.git/
├── config/
│   ├── manager.json             schema, root UUID, identities, fixed policy
│   └── viewfs.json              generated git-viewspace-fs alias configuration
├── state/
│   ├── manager.json             durable root and view registry
│   ├── transactions/            pending durable operations
│   └── lock                     stable daemon flock inode
├── run/
│   ├── manager.sock             recreated private control socket
│   └── runtime.json             optional diagnostic observations
├── git-mnt/                     one git-viewspace-fs mountpoint
└── views/
    └── <view-name>/
        ├── config/
        │   └── view.json        immutable definition and desired policy
        ├── state/
        │   └── state.json       mutable view transition state
        ├── overlay/             writable views only
        │   └── generations/
        │       └── <generation-id>/
        │           ├── generation.json
        │           ├── upper/
        │           └── work/
        └── mnt/                 public read-only or writable mountpoint
```

The repository also contains stable ownership records:

```text
<root>/repo/.git/git-viewspace.lock
<root>/repo/.git/git-viewspace-owner.json
```

Rules:

- The root and repository owner records contain the root UUID and canonical
  root path.
- `state/manager.json` is the authoritative view registry. Directory listing
  is a consistency check, not a second registry.
- Every durable record contains a format magic, schema version, root UUID, and
  its view or generation identity where applicable.
- Per-view state names the active generation. There is no `active` symlink and
  no inference from directory order.
- `config/viewfs.json` is derived from authoritative state and can be rebuilt.
- `run/` is never authoritative. Missing runtime diagnostics do not alter
  durable meaning.
- `git-mnt/` and every `mnt/` are empty ordinary directories while unmounted.
- A read-only-only view has no `overlay/` directory.

## 5. Repository invariants

`repo/` is both the git-viewspace-fs object source and the singular checkout used by
sync, stage, and commit. The manager creates no secondary worktree.

The repository must remain:

- local, complete, non-bare, and SHA-1;
- on the branch/ref state expected by the active operation;
- free of merge, rebase, cherry-pick, revert, bisect, unresolved-index, or
  similar in-progress state; and
- exclusively owned by this root.

After initialization, external working-tree edits and direct Git commands are
unsupported. Daemon startup performs complete repository qualification and
the checkout ownership walk. Online operations revalidate only the HEAD,
branch, index, checkout, manifest, or tree facts that can affect that specific
transition; they rely on the daemon's lifetime locks, authoritative in-memory
state, and matching durable state rather than repeating startup qualification.

The manager resolves a direct local parent and creates a new direct local attempt branch only as the first phase
of compound `view init-and-start overlay`. It reports and retains the branch if a later compound
step fails. The narrow repository-only `branch merge` operation computes an explicit two-parent
commit without view mutation, then compare-and-updates only an unclaimed parent ref. Its explicit
policy may detach an exact clean parent checkout without rewriting the index or worktree before
the ref update. It retains the attempt ref. The manager never deletes, rebases, pulls, pushes, fetches, or
force-updates branches. The standalone writable upgrade claims one existing direct local branch, and
exactly one view may own a branch.

## 6. Configuration and durable state

Configuration records desired policy; state records durable facts needed for
recovery.

Root configuration includes:

- root UUID and schema;
- canonical repository identity;
- workload UID/GID policy;
- controlled Git author and committer identity; and
- fixed FUSE, OverlayFS, and filesystem policy.

Root state includes the view registry and repository-wide operation state.
View configuration includes the validated name, pinned commit, immutable
alias, optional branch ownership, and desired-started policy. View state
includes the actual durable base, active generation, and optional sync, stage,
and next-transition records.

All JSON is canonical UTF-8 with strict schemas. Unknown required fields,
wrong types, transplanted identities, and unsupported schema versions fail
loudly.

## 7. Durability and recovery boundary

Individual JSON records are written by atomic sibling replacement and followed
by file and parent-directory durability operations. Initialization additionally
uses an explicit transaction record because it must publish an entire managed
root and, for `--move`, transfer ownership of an existing repository.

The implemented view lifecycle does **not** wrap every multi-file or
filesystem-plus-Git transition in a general write-ahead transaction. The
published `sync`, `stage`, and `next` phases are durable resume points, and a
command may be retried when the complete retained records and observed Git
state unambiguously describe one of those phases. The daemon also records a
non-ready git-viewspace-fs runtime identity before restoring public mounts, allowing
a later daemon to remove mounts left by an interrupted startup or child
replacement.

A process or host failure between separate authoritative publications may
instead leave a state that the manager cannot repair automatically. Relevant
windows include a hard death after the kernel creates an git-viewspace-fs mount but
before child start returns and publishes its source, view creation/destruction,
writable initialization or deinitialization, generation clean/switch, a Git
commit before its `next` record, and a view-state phase record before the
corresponding root registry record. Startup rejects those partial states
rather than guessing which side of the transition was intended.

Providing automatic recovery for every such window would require a broader
transaction journal and operation-specific reconciliation framework. That is
outside the MVP hardening boundary. Operators must preserve the root for
diagnosis and either perform a documented, case-specific repair or recreate
the managed root from its Git repository and any separately salvaged upper
content. Cleanup failure is never reported as successful cleanup.

## 8. Locks and serialization

`state/lock` and `repo/.git/git-viewspace.lock` are stable inodes. They are
created once and never replaced or unlinked during the managed root's
lifetime.

- Initialization holds the root lock for the entire transaction. It creates
  the managed repository's stable lock and claim before publishing that
  repository into the root. Concurrent external mutation of a `--copy` or
  `--move` source is outside the MVP contract.
- The daemon holds both locks from startup through complete shutdown.
- One worker serializes mutations, supervision and full state observation. The selector
  frontend never reads mutable Git/view/mount state.
- Status concurrently serves a frozen consistent observation with age and current activity.
  A short mutex protects only in-memory snapshot/activity exchange, not filesystem work.
- Different roots may operate concurrently only when their repositories,
  mounts, sockets, and storage are disjoint.

## 9. View model

Every view begins with a read-only definition:

- one immutable pinned commit OID;
- one unique immutable git-viewspace-fs alias; and
- one desired-started flag.

A view may additionally have writable state:

- one exclusively owned branch;
- an overlay base equal to the pinned commit;
- one active marked upper/work generation; and
- optional sync, stage, and next-transition records.

Actual runtime state is independent. A view may be stopped, started read-only,
or started writable. Desired state expresses whether daemon recovery should
restore its public mount.

Read-only views may switch to any representable commit while stopped. Writable
views may switch only to the current tip of their owned branch and only after
the upper is clean. An upper generation is never reused with a different lower
commit.

## 10. git-viewspace-fs aliases

`git-mnt/` contains the one supervised git-viewspace-fs mount. Its generated config
maps opaque aliases to exact commit OIDs.

An alias:

- is allocated by the manager, not supplied by the user;
- maps to exactly one commit for its entire published lifetime;
- is published before any mount refers to it;
- remains pinned while durable state or a retained transaction may reference
  it; and
- is removed only after no mount, state, transaction, or recovery path can use
  it.

Retiring or switching a stopped view does not restart git-viewspace-fs. The unique
old alias is removed from manager configuration and is unreachable from any
public mount. Once resolved, a process-local alias remains pinned until
ordinary git-viewspace-fs recovery or daemon shutdown so an in-flight lookup cannot
observe a changed meaning. The current registry does not evict retired pinned
aliases during one git-viewspace-fs process lifetime. Each mapping is small, but a
daemon that performs very many view switches accumulates private alias names;
an orderly daemon restart clears them. Runtime eviction or automatic periodic
restart is deferred because either requires an explicit open-handle or
availability policy.

The daemon verifies an alias can expose its commit before mounting a public
view. git-viewspace-fs configuration is rebuilt atomically after validation of the
complete alias set.

git-viewspace-fs must remain structurally lazy: mount and view creation cannot walk
or materialize the selected Git tree. Lookup, attribute, and content caches
may retain immutable commit/tree metadata within bounded policies.

## 11. Mount profiles and permissions

git-viewspace-fs is mounted read-only with the qualified Stage 0 behavior, including
stable workload ownership, Git-compatible modes, `allow_other`,
`default_permissions`, and disabled negative lookup caching.

A read-only view publicly exposes its immutable alias through a manager-owned
read-only mount at `views/<view>/mnt`.

A writable view mounts OverlayFS with:

- lowerdir equal to the exact immutable git-viewspace-fs alias;
- upperdir and workdir from the active generation;
- upper and work on the same qualified ext4 filesystem; and
- the fixed options recorded by the qualified profile.

The workload identity uses mounted `mnt/` paths for ordinary repository work.
Direct access to `repo/`, manager state, raw overlay storage, or control paths
is unsupported operator behavior. The MVP does not add a separate service
identity or filesystem namespace merely to prevent the same local user from
doing so.

Mount creation and removal are privileged. Ordinary file mutations under a
public view run as the workload identity. Read-only privileged inspection may
classify mount state and stopped OverlayFS metadata. Checkout replay inventories
privileged metadata first, then irreversibly drops supplementary groups, GID,
and UID before changing checkout paths.

## 12. Overlay generations

Every writable generation contains sibling `upper/`, `work/`, and
`generation.json`. The marker binds the generation to the root UUID, view,
generation ID, base commit, and schema.

- A new generation begins with empty upper and work directories.
- Remounting an existing upper reuses its paired work directory; work contents
  are kernel-private and are not interpreted or replayed.
- Dirty state means any persistent upper entry or metadata beyond the exact
  qualified empty-upper baseline.
- Cleaning creates and publishes a fresh generation before retiring the old
  one.
- Retired or provisional generations remain explicitly named until exact
  state and transaction validation permits removal.
- No manager marker is stored inside upper or work, where it could become view
  data.

## 13. Overlay artifact and Git semantics

The accepted upperdir grammar and replay precedence are defined by
[`OverlayFS-artifact-format.md`](OverlayFS-artifact-format.md). In summary:

- qualified whiteouts delete the corresponding lower path;
- opacity removes inherited lower children before applying upper children;
- qualified redirects move a lower directory source before applying upper
  children and nested whiteouts;
- ordinary upper regular files and symlinks replace any lower type;
- ordinary upper directories merge with lower directories unless replacement
  or opacity semantics apply; and
- origin and impure metadata are validated bookkeeping, not Git identity.

Git-relevant output preserves exact relative path, inode class, regular bytes,
the owner executable bit as Git executable class, and exact symlink target.
Ownership, timestamps, inode identity, allocation, directory modes, ordinary
non-executable permission details, and admitted ignored xattrs do not enter the
Git tree.

Unknown private metadata, unsupported inode types, hard-linked upper entries,
invalid UTF-8, `.git` components, gitlinks, malformed redirects, and ambiguous
redirect graphs fail before checkout mutation.

## 14. Sync, stage, commit, clean, and switch

The primitive command contract is defined in
[`git-viewspace-specification.md`](git-viewspace-specification.md). The architectural
requirements are:

### Sync

- The view is writable, stopped, undesired, and based on the owned branch tip.
- The checkout and index are reset to the recorded base.
- The complete stopped upper is validated before replay.
- Replay changes only paths described by the validated plan.
- A durable generation/base/upper-bound literal Git path manifest is recorded.

### Stage

- The sync record, branch, checkout, index, generation, and manifest
  must still match.
- Only the literal NUL-delimited manifest is staged with `git add -A --force`.
- The manager verifies no Git-relevant changed path was omitted.
- The resulting index tree is recorded durably.

### Commit

- The stage record and expected parent still match.
- A changed tree produces one commit with the expected sole parent and
  controlled identity.
- An unchanged tree is a Git no-op and creates no empty commit.
- The result is recorded as a pending `next` transition; commit alone does not
  reuse the dirty upper with the new base.

### Clean and switch

- Clean explicitly discards the old upper and resets checkout/index while
  preserving a valid `next` result.
- Switch publishes a fresh immutable alias and, for writable views, a fresh
  empty generation against the new base.
- The consumed transition is then cleared.

## 15. Daemon startup and recovery

Before publishing its ready socket, the daemon:

1. acquires root and repository locks;
2. validates root, repository, registry, every view, and every generation;
3. rejects initialization transactions and classifies manager-owned runtime
   residue;
4. rejects foreign or ambiguous mounts/processes;
5. preflights all desired view aliases and mount plans;
6. reconstructs git-viewspace-fs configuration;
7. starts and verifies git-viewspace-fs; and
8. restores desired views in deterministic order.

Readiness is all-or-nothing. After git-viewspace-fs starts, the daemon publishes its
exact child and mount identity with `ready=false` before restoring public
views. If restoration fails, startup attempts to roll back mounts created by
that attempt and stop its git-viewspace-fs child. It removes the runtime record after
complete cleanup, but retains the non-ready record when cleanup itself fails,
so the next startup can identify rather than guess at the residue.

The daemon supervises git-viewspace-fs. If the child fails, the daemon stops or
detaches affected public mounts according to the qualified recovery procedure,
reconstructs the same alias configuration, launches a replacement child, and
restores desired views only after validation. It never remounts a dirty upper
against a different commit.

## 16. Shutdown

Whole-daemon `stop` is the forceful cleanup boundary defined by the CLI spec.
It stops admission, drains the admitted mutation, removes every public view
mount, unmounts git-viewspace-fs, terminates manager-owned children, removes runtime
artifacts, releases locks, and exits. Per-view desired-started flags are
retained so a later daemon can recover them.

An individual `view stop` is different: it uses orderly unmount and returns a
busy error instead of force-detaching the view.

Success from whole-daemon stop means no manager-owned mount or child remains.
Failure reports the exact residual state; it never claims clean shutdown while
residue remains.

## 17. Path and cleanup safety

All manager paths are derived from the canonical root and validated view or
generation identifiers. Management modules use a small internal path/system
boundary and extend it before adding a new direct host operation.

Before destructive cleanup, the manager revalidates:

- root magic, schema, UUID, and repository claim;
- exact structural components with no symlink following;
- view and generation markers;
- mount topology and filesystem boundaries; and
- the target's exact manager-owned role.

Recursive cleanup never follows a symlink, crosses a mountpoint, crosses into
another filesystem merely because a path is nested, or targets the root,
repository, shared views directory, or an unvalidated glob.

## 18. Output and errors

Every command supports the human and JSON modes defined by the CLI spec. JSON
results contain a stable command/result shape, root/view identity where
applicable, and a symbolic error code on failure.

At minimum, distinct errors exist for:

- invalid arguments or names;
- uninitialized or inconsistent root;
- daemon absent, already running, or wrong owner;
- repository claim, lock, topology, or schema failure;
- view missing, already defined, wrong kind, started, stopped, busy, or dirty;
- branch missing, claimed, or divergent;
- exact attempt-tip mismatch, checked-out-parent policy refusal, dirty parent checkout,
  parent-tip movement, or branch-merge conflict;
- alias, generation, mount profile, or runtime residue mismatch;
- stale sync, stage, checkout, index, manifest, or next transition;
- unsupported OverlayFS artifact or Git representation; and
- partial cleanup or recovery required.

Diagnostics do not disclose uncontrolled file contents or claim authority from
PID files alone.

## 19. Testing and acceptance

Each command stage adds unit tests, transaction-boundary tests, and focused
mount-enabled end-to-end tests. The retained Stage 0 study and OverlaySync
repository scenarios remain the replay oracle.

MVP acceptance requires:

- all initialization modes produce the same valid root and retry correctly;
- daemon start, git-viewspace-fs supervision, recovery, and forceful stop leave exact
  state;
- multiple independent roots run simultaneously without interference;
- read-only and writable views initialize, mount, stop, recover, switch, clean,
  deinitialize, and destroy correctly;
- ordinary workload access succeeds only through public mounts;
- every qualified upper scenario syncs to the independent Git tree;
- targeted staging equals independent full staging;
- changed and no-op commits transition through fresh aliases/generations;
- branch integration advances only an unclaimed parent ref, preserves the attempt and its view,
  reports bounded repository-relative conflicts, and recognizes replayed success;
- every implemented resume boundary has deterministic retry tests, while
  unsupported partial-publication states fail closed;
- unknown artifacts and stale state fail before destructive effects; and
- a fresh supported host passes installation, service, performance, and
  complete integration validation.

The active command-oriented Stage 0–8 implementation sequence is maintained
only in Section 14 of
[`git-viewspace-specification.md`](git-viewspace-specification.md). No separate speculative
stage documents are authoritative.
