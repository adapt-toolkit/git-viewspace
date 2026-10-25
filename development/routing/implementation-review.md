# Independent implementation review

A fresh read-only reviewer examined the native router, manager lifecycle, runtime
mounts, tests and performance evidence for correctness, simplicity and hidden
ordinary-file costs. This record tracks resolutions; it is not a claim that all
qualification work is complete.

## Adopted corrections

- Recovery now discovers private router mounts, process records and runtime bind
  records even if the public view mount is absent. It still refuses recovery
  without the owning daemon runtime record. Unit tests cover owned and unowned
  private-only residue.
- Directory fsync forwards to the retained underlying directory handle instead of
  relying on FUSE's default response. Mounted acceptance exercises directory fsync.
- Failure to persist or sync an identity fault terminates the helper. The manager
  marks abnormal termination uncertain and requires explicit rebind. Mounted
  acceptance denies fault-directory writes and verifies this recovery boundary.

## Deliberately retained boundary

The reviewer identified that default POSIX record locking is local to the FUSE
mount, despite earlier wording suggesting all advisory locking was rejected. We
retain ordinary kernel FUSE record locking (needed by applications such as SQLite)
and document the external-file limitation rather than build a cross-view lock
manager. Special-file flock is rejected. A mounted subprocess test demonstrates
that a direct backing-file open can acquire a POSIX lock while the routed path is
locked. Applications requiring coordination through those locks are unsupported.

## Additional retained acceptance

The mounted router test covers mixed read/write opens and copy-up; old open handles
through rename, unlink and pathname reuse; external contents excluded from commits;
identity faults persisting through restart; and explicit rebind. Runtime-directory
acceptance now tests renaming a protected ancestor that is not itself a mountpoint,
so the assertion is not merely the kernel's EBUSY protection for mounted paths.

## Follow-up recovery corrections

The follow-up review found that a disconnected public FUSE mount could prevent
root loading before reconciliation. Loading now defers only ENOTCONN on a routed
mount matching the validated view identity; reconciliation still requires the
owning runtime record. Uncertain recovered views remain stopped so the control
daemon can start and accept an explicit rebind. The uncertainty marker is written
atomically and synced before removing the process record; its directory is synced
again afterward.

Mounted acceptance now kills the disposable manager after the helper exits on a
failed fault write, then starts a replacement manager against the disconnected
mount. Cleanup, the stopped/faulted state and explicit rebind all pass. Runtime
unit tests also cover ownership retained after remount failure, busy stop retaining
its record, and refusal to unmount unrecorded runtime mounts.

## Qualification

Follow-up review found no further blockers in the recovery changes. The complete
registered exhaustive/full-profile run passed 118 cases, skipped four interpreter
matrix cases for unavailable prerequisites, and had no failures or infrastructure
errors. All five selected host acceptance cases passed, including existing
contention/submodule tests and the new router/runtime/origin tests. The clean basic
run passed 39 cases with no skips.

A separate fresh non-editable installation passed routed-file failure/recovery and
runtime socket acceptance, with no remaining mounts. Eight further installed
restart cycles alternated routed and ordinary views. Performance evidence and its
limits are recorded in performance.md. Live harness qualification is separate and
is not implied by filesystem acceptance.

The Python 3.13 source-and-wheel matrix case subsequently passed after installing
its missing build dependencies. Python 3.11, 3.12 and 3.14 were unavailable on this
host and remain unqualified by this run.

## Dynamic routing and subtree follow-up

Independent read-only review required and verified these changes:

- Serialize namespace checks and mutations against descriptor installation; reject
  open or hardlinked rename sources so no writable alias survives admission.
- Directory listing parses local descriptors without opening/admitting backing files;
  retain a correct per-directory pagination snapshot and serialize shared-handle use.
- Use declaration-based admitted identity across directory moves; unreadable identity
  records fail closed. Preserve legacy admitted identities and faults on upgrade.
- Distinguish runtime roots from protected ancestors so sibling descriptors work.
- Origin reset epochs include unique lower-FUSE incarnation, revision/alias and upper
  generation, and are persisted only after successful cleanup.
- Narrow public routing roots preserve the full underlying overlay/commit. Runtime
  marker checks retain repository prefixes; submodule-root selection is rejected.
- Append helper diagnostics with attempt timestamps rather than overwrite on restart.

The final read-only pass found no additional routing correctness blocker. Regression
and benchmark results are recorded separately; review does not substitute for them.
