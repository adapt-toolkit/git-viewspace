# Daemon and recovery

## One daemon per managed root

Each initialized root owns one repository and, while running:

```text
git-viewspace daemon
├── one private control socket
├── one supervised git-viewspace-fs child and private FUSE mount
├── zero or more public read-only bind mounts
└── zero or more public writable OverlayFS mounts
```

Different roots can run independent daemons. Within one root, the daemon is
the sole controller of the checkout, view records, branch claims, generations,
and mounts. It serializes mutations; the MVP does not execute concurrent
manager mutations within one root.

The ordinary user is expected not to modify `<root>/repo` or manager storage
directly. The public workload interface is `<root>/views/<view>/mnt`.

## Startup

`git-viewspace start` performs deep validation once at daemon startup. It checks the
root, repository, durable registry, every view and generation, recognized
runtime residue, aliases, and desired mount plans before reporting readiness.

It then starts one persistent git-viewspace-fs process for the repository. All views
share that process through immutable commit aliases. Read-only public views are
bind mounts of aliases; writable views place OverlayFS above an alias.

The daemon records a view's desired mount state separately from the currently
observed mount. On startup it restores all views whose durable
`desired_started` value is true.

## Online validation

Routine online commands do not repeat the startup-time repository ownership
walk, object connectivity check, or complete Git-tree qualification.

The client checks the root identity, selected view record, runtime process, and
control socket. The daemon then compares the relevant durable records with its
in-memory state. If an existing-view request supplies `ensure_view_id`, the
daemon next compares it with that reconciled realization and returns
`VIEW_ID_MISMATCH` before dispatch when they differ. Only then does it execute
the command's state-machine, mount, upper, or Git checks. This keeps small view
operations inexpensive without treating the in-memory state as the only
durable authority.

The private socket protocol is strict version 4. Upgrade client and daemon together;
version 3 is rejected, never inferred or upgraded. Mutations may include a finite positive
`queue_timeout_seconds` (maximum 86400; raw default 300). The client supplies its selected
timeout. This bounds server queue residence after complete admission, not execution.
For example, the repository-only `branch-pin` request is:

```json
{"protocol":4,"command":"branch-pin","branch":"viewspace/publications/r.../000042","commit":"<40-lowercase-hex-commit>","queue_timeout_seconds":180}
```

Success echoes the exact requested identities and reports whether this call created the ref:

```json
{"ok":true,"command":"branch pin","root":"<root>","root_uuid":"<root-uuid>","branch":"viewspace/publications/r.../000042","commit":"<commit>","created":true}
```

The validator rejects missing, extra, or wrongly typed fields. A structured failure uses command
`branch-pin`; the CLI and Rust facade validate command and root identity before accepting it. One
daemon request owns the complete serialized pin or merge operation under the daemon-lifetime
repository lock.

`status` performs deeper read-only inspection when no daemon is available.

## Responsive status and bounded admission

One selector frontend admits at most 128 connected peers and 64 queued mutations.
One worker owns mutation, observation, supervision and shutdown; Git writers never overlap.
Incomplete requests and undeliverable responses expire after five seconds total. Each
connection carries one request; do not half-close the sending side before its response.
Closing an undispatched request abandons it. Closing a dispatched request does not cancel it.

Online status and view-status use a frozen observation, not concurrent filesystem reads.
`observation` gives `kind:last_consistent_snapshot`, capture Unix time and monotonic age.
`activity` gives the active command/view/phase/elapsed time, completed phase timings, queued
count and shutdown state. Snapshot fields, including health, describe the capture moment;
they are not claims about an in-flight transition. A newly created view appears after its
operation finishes. Failed observation is a visible error, not an old healthy status.
Read-only clients validate root/process/socket identity but do not read live view records;
they can query the control endpoint during mount recovery (`ready:false`).

Snapshots refresh after operations and periodic idle supervision. Polling does not launch
Git. Successful replies include queue/service timing. Failure metadata survives service/CLI
translation in `error.data`, including timing and, for status failures, observation/activity.
`executed:false` means refusal/expiry before dispatch; `executed:true` means dispatch began,
not necessarily that a mutation succeeded. A transport timeout after dispatch is uncertain:
inspect committed state before retrying. There is no replay ledger or exactly-once promise.
Queue/snapshots are in-memory only. Long executing Git operations have no new automatic
cancellation deadline and may require operator intervention; status remains responsive.

The uninterrupted `view advance` path reuses the base-index proof just established by sync
and the index tree just established by stage. It omits one mixed reset and two repeated index
reads, while retaining branch/HEAD, manifest completeness, final tree and ancestry checks.
Independent stage/commit calls and resumed transitions retain full verification. Proofs are
local variables, never restart state or public switches. Submodule registration parsing has a
128-entry process-local cache keyed by canonical repository location and exact commit OID;
mutable refs, failures and module-store availability are never cached.

## git-viewspace-fs recovery

The daemon supervises its git-viewspace-fs child. If the child fails, the daemon
reconciles affected public mounts, rebuilds the alias configuration, starts a
replacement child, and restores desired views after validation. It never
mounts an existing dirty upper against a different commit.

Immutable aliases make a view's lower stable for the lifetime of a generation.
Switching a view publishes a new alias rather than changing the meaning of an
old alias.

An alias that git-viewspace-fs has already resolved remains pinned privately for that
git-viewspace-fs process lifetime, even after it is removed from durable
configuration and no public mount uses it. The mapping is small and cannot
change meaning, but there is currently no in-process retirement bound. An
orderly daemon stop/start clears retired aliases. Automatic eviction or
periodic child restart is deferred because it would require an explicit policy
for open handles or transient view disruption.

## Durable writable transitions

A writable transition can persist these phases:

- `sync`: stopped upper fingerprint, generation, base, and path manifest;
- `stage`: the exact Git index tree derived from that sync; and
- `next`: the commit/no-op result awaiting clean and switch.

The primitive commands can be retried at these boundaries. `view advance`
uses the same records and resumes rather than discarding a completed phase.

Only one view in a root can own the singular repository transition at a time.
Other views may remain mounted, but another sync/stage/commit transition waits
until the current transition is cleaned and switched or explicitly discarded.

## Shutdown

`view stop` is an orderly per-view operation. If the mount is busy, it reports
failure and preserves the view.

Root-level `stop` is intentionally forceful after the active operation completes.
Acceptance stops mutation admission, refuses queued unstarted requests and cannot be
cancelled by disconnecting its client. SIGTERM uses the same drain boundary. Status stays
available while the current operation finishes. The worker then removes all
public mounts, unmounts git-viewspace-fs, terminates manager-owned children, removes
the socket/runtime record, and releases locks. It retains durable view
definitions and desired states so the next `start` can recover them.

A successful root stop means no manager-owned mount or subprocess remains.
If cleanup is incomplete, the command reports failure rather than claiming the
root is stopped cleanly.

## Current hardening boundary

Core daemon recovery, git-viewspace-fs supervision, desired-mount restoration,
durable completed-phase retry, and whole-root cleanup are implemented and have
focused mount-enabled tests. Startup writes a non-ready runtime identity as
soon as git-viewspace-fs starts and refreshes it as soon as a replacement child
starts. That gives a later daemon the exact mount source needed to reconcile a
crash during public-view restoration.

The MVP does not have a general transaction journal for every operation that
changes more than one durable file or combines a filesystem change with a Git
change. It therefore fails closed, rather than guessing, if a process or host
failure lands inside certain narrow publication windows. Examples include:

- a hard daemon death after the kernel creates an git-viewspace-fs mount but before
  the child start returns and its source is published;
- view creation or destruction between the view registry and view directory;
- writable initialization/deinitialization between overlay storage and view
  records;
- clean or switch between generation/config/state publications and retirement
  of the prior generation;
- a Git commit that advances the branch before its durable `next` record; and
- a view phase record written without its matching root repository-operation
  record.

An interrupted command should first be retried only when `status` and the
durable phase clearly identify a supported resume point. If startup or status
reports inconsistent durable state, stop workload access, preserve the root
for diagnosis, and do not delete unexplained generations or mounts by hand.
Case-specific repair or recreation of the managed root from its repository is
required. Complete automatic repair of these windows would add a substantial
transaction and reconciliation subsystem and is intentionally deferred.

Stage 8a was limited to low-cost correctness and cleanup improvements without
repository-wide hot-path checks. Stage 8b packaged that implementation and
added fresh-environment guidance, service-manager examples, and documentation
consistency without new runtime semantics.
