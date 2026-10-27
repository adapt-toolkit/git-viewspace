# Recursive pinned submodules

## Scope and contract

Extend the existing explicit, read-only pinned-submodule support to nested
submodules. Preserve ordinary Git gitlinks and snapshot isolation. Include the
already committed narrow routing long-filename fix on this work branch. Do not
change production deployments, other repositories, writable-submodule semantics,
or the daemon's general execution architecture. Publish this branch only.

1. `submodule prepare --at REF --path TOP [--commit OID] --recursive`
   explicitly acquires the selected module and its transitive pinned descendants.
   Without `--recursive`, nested modules produce a clear unsupported error as
   before. Existing requests without the new option remain valid.
2. A view always exposes the full pinned tree if all objects have been prepared;
   startup and reads never fetch. Missing descendants fail before mounting.
3. Registrations come from each containing commit's `.gitmodules`, never mutable
   local submodule config. Descendant paths are relative to that repository.
4. Resolve `./` and `../` URLs relative to the containing repository URL using
   Git's repository-as-directory convention. Support absolute local paths,
   URL-form remotes and SCP-style SSH remotes. For a relative top-level URL use
   the owning repository's origin URL, falling back to its absolute local path
   when no origin exists. Freeze this base on first relative preparation in
   root-owned `viewspace-submodules/source.json`; later origin changes or moves
   do not reinterpret prepared pins. Reads cannot create this binding. Absolute
   registrations need no base binding. Resolve an initially relative local
   origin against the repository location. Reject unsafe/unsupported URL forms explicitly; never
   invoke a shell or expose credentials in progress records.
5. Store all acquired repositories under the root repository's existing
   URL-keyed bare-store directory. Retain each exact commit with `refs/pins/OID`.
   Reuse available commits; deduplicate visits by resolved URL and commit, not
   URL alone. Separate reuse from cycle detection along the active ancestry.
6. Validate complete closure locally before pin publication or view startup.
   Traversal is bounded (depth 32, 4096 module occurrences) with explicit errors.
   Missing, malformed and unsupported objects fail closed. Interrupted fetches
   never publish a parent pin update; successfully prepared objects are reusable.
7. Acquisition shares a 120-second overall fetch/traversal budget, preserving the
   current 180-second RPC allowance. Each fetch receives remaining time and its
   process group is reaped on timeout. All preparation-owned Git subprocesses, including local metadata/validation,
   receive the remaining deadline. Progress is an atomically replaced, bounded
   `run/submodule-prepare.json` (path, commit, phase, counts, timestamps and
   terminal success/error code; no URLs or raw Git errors). A running record
   left by a stopped daemon is stale diagnostics, never restart authority.
   Prepared/reused totals are returned. No replay journal is needed. RPC queue
   waiting remains subject to its existing independent timeout behavior.
8. FUSE resolution carries containing repository, module commit and module-root
   path across gitlinks. Cache immutable metadata by repository/commit and retain
   repository+blob identity for contents. Avoid full descendant validation on
   every file read; preflight validates the closure, lazy lookup resolves only
   the relevant gitlink with bounded metadata caches.
9. Top-level read-only bind mounts already protect all descendants in overlay
   views; do not add nested mounts. Routing/runtime-directory protection and
   sync guards continue applying to entire top-level module subtrees.
10. Pin updates remain top-level operations. Update nested pins by committing in
    their owning repository and then admitting a new outer commit. Existing
    views keep their old nested pins, including when another view admits updates.

## Stages and gates

### 1. Independent plan review

Review this document and existing acquisition, Git validation, FUSE lookup,
CLI/protocol, mount/routing and tests. Focus on correctness, simplicity, scope,
URL semantics, failure/retry, snapshot isolation, traversal/resource bounds and
test coverage. Record findings and resolutions here before implementation.

### 2. Metadata, URL resolution and closure traversal

Separate raw committed registrations from resolved acquisition locations. Add a
shared bounded closure resolver with root-owned store addressing and explicit
per-containing-repository context. Local availability and acquisition must use
the same URL/path rules. Keep top-level `entries` consumers compatible. Test
three levels, diamonds, repeated URLs at distinct commits, relative URLs against
Git-generated fixtures, missing registrations, depth/occurrence guards and cycles
(synthetic graph where a real commit cycle is impractical).

### 3. Preparation and public command integration

Add recursive option through CLI, service, strict protocol and daemon. Preserve
old wire requests. Implement budgeted explicit preparation with reuse, progress,
pin retention and safe retry. Test option propagation, malformed flags, partial
failure/retry, no hidden network, deadline/process-group cleanup and pin refusal
when descendants are missing. Document the unchanged top-level pin boundary.

### 4. Reader and lifecycle integration

Extend repository context in resolved tree entries and metadata caches. Read
nested blobs, executable bits, symlinks and directory listings. Verify cache
separation across snapshots and stores. Reuse top-level protection mounts and
guards. Tests must prove ordinary commits preserve gitlinks and nested module
content is never flattened or modified through a view.

### 5. Qualification and documentation

Run focused tests and the repository basic suite, then disposable real FUSE/
OverlayFS host tests under `~/.scratch`: three-level nesting; concurrent old/new
pins; readonly and writable views; routed protection; offline operation after
preparation; advance; daemon restart; complete cleanup. Use the current source
installed into a scratch venv, not an older installation. Run existing routing
and subtree tests to guard the earlier filename fix and mount interactions.
Document CLI examples, URL base selection, recursive behavior, budgets, errors,
read-only constraints and explicit preparation. Register new tests in the
standalone harness. Record commands and receipts, including limitations.

### 6. Independent implementation and coverage review

Read-only reviewer checks the final diff against this contract and inspects test
coverage, including negative cases. Fix findings and re-run affected tests;
obtain reviewer follow-up on material findings. Commit implementation, tests,
documentation and review evidence on the work branch. Verify clean state and
push branch to origin; do not merge main or deploy.

## Completion evidence

- Reviewed plan and resolved findings committed before implementation.
- Unit/integration and real-mount results with no remaining mounts/processes.
- Independent implementation review and finding dispositions.
- Clean worktree; remote branch tip equals local final commit.

## Plan review disposition

Independent read-only reviewer: recursive_plan_review. Initial verdict: amend
before implementation. All findings accepted:

- Freeze root-relative URL base in local repository-owned metadata; preserve it
  across origin changes and repository relocation. Raw registration caching
  remains repository/commit keyed; resolved URLs always incorporate containing
  URL. Test identical containing commits under different origins.
- Bound all preparation subprocesses, not merely fetch. Add metadata timeout
  regression and keep process-group cleanup for acquisition.
- Use observable bounded progress with terminal outcomes and documented stale
  diagnostics semantics, not discarded stderr.
- Extend tests for unchanged branch on failure, retained/reused pins, occurrence
  counting despite deduplication, nonrecursive rejection even after preparation,
  routed nested rename/unlink rejection and no residual mounts on missing pins.

Follow-up verdict: **approved** before implementation. The reviewer additionally
requires restrictive permissions for `source.json`, since origin URLs may contain
credentials; URL values must not appear in progress or public error details.

## Implementation review dispositions

Independent read-only reviewer: recursive_implementation_review.

- P1 accepted: commit/tree presence alone did not prove blob availability. Added
  batched blob identity/type checks to module validation, under the preparation
  budget. Added missing-blob rejection during cached preparation, pinning and
  view preflight. Avoid payload decompression and per-blob subprocesses.
- P2 accepted: whole-path normalization differed from Git relative URL semantics.
  Consume leading relative components only, preserving interior spelling.
  Explicitly reject traversal above URL/path roots, rather than allowing a
  relative location to change a host. Added Git parity and rejection cases.
- Mounted acceptance found progress filename missing from runtime admission;
  recognize and validate the singleton diagnostic file on restart.

Follow-up verdict: **approved**. Reviewer reran the expanded unit tests (16
including the imported legacy tests) and confirmed both findings resolved, with
no remaining actionable findings. Parent remains responsible for mounted
qualification and final branch publication.
