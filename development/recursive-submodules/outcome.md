# Recursive submodules: implementation and qualification

## Delivered behavior

- Explicit `submodule prepare --recursive`, backward-compatible optional RPC flag.
- Shared URL-keyed stores, exact retained pins, reuse and partial-failure retry.
- Bounded closure traversal with ancestry-cycle detection and occurrence counting.
- Git-compatible leading relative URL resolution; above-root traversal rejected.
- Frozen root URL base in private local metadata; containing URL retained through
  recursive file resolution and included in resolved registration cache identity.
- Read-only recursive exposure in ordinary and routed views, no nested mounts.
- Shared preparation deadline, fetch process-group cleanup, bounded observable
  progress in `run/submodule-prepare.json` with terminal outcomes.
- Batched blob availability checks before preparation acceptance/pinning/mounting.
- Existing top-level pin update and sync boundaries retained.

## Independent reviews

The plan was reviewed and approved before implementation (commit `5e41f68`).
`plan.md` records all initial findings and their dispositions. An independent
implementation/coverage review requested two corrections: incomplete blob
validation and relative URL normalization. Both were implemented, tested and
approved on follow-up; no actionable findings remain. Reviewers were read-only.

## Test procedure

Use a fresh installation of this checkout in a scratch virtual environment with
`requirements-test.txt`. Set `PATH` to that environment. Build the routing helper
from this branch, and set `VIEWSPACE_ROUTER_BIN` to its absolute path. The host
used for qualification is Linux, Python 3.13, ext4, with actual FUSE/OverlayFS and
passwordless mount privileges. All host tests use disposable repositories.

```sh
python utils/viewspace_test.py run --scope full \
  --scratch-root "$SCRATCH/f2" --report "$SCRATCH/qualified.json" --html-report "$SCRATCH/qualified.html"

python utils/viewspace_test.py run --scope exhaustive --profile full \
  --glob '*router-host*' --glob '*subtree-host*' --glob '*submodule-host*' \
  --scratch-root "$SCRATCH/hosts" --report "$SCRATCH/hosts.json"

# Also exercise the freshly installed final package directly, without PYTHONPATH:
python apps/git-viewspace/tests/run-recursive-submodule-host.py
```

The registered recursive tests cover real Git objects, exact pins, three levels,
shared dependencies, multiple pins, missing blobs/stores, malformed registrations,
cycle/depth/occurrence limits, relative URL parity with Git, stable root base after
origin changes/relocation, preparation deadlines, progress failures and retry.
The mounted case proves simultaneous old/new snapshots, readonly/overlay/routed
protection against write/unlink/rename, executable/symlink reads, data-only
commits preserving gitlinks, offline restart and missing-descendant mount refusal.
Existing router qualification also proves the narrow long-filename regression.

An intermediate basic run caught broad unittest registration; descriptors now
list exact methods. An intermediate full run passed all 85 cases but was rejected
because a regression definition changed during execution; it is not used as a
completion receipt. The frozen final run supersedes it. The mounted test caught
and drove correction of runtime admission for the new progress diagnostic.

## Intentional boundaries

No production deployment or merge to main. Nested module edits remain read-only;
updating nested pins requires commits in their owning repositories. Acquisition
uses existing Git credentials; no live remote credentials are needed by tests.
Above-root URL traversal and external remote helpers are explicitly unsupported.
The local object stores and frozen source binding have repository lifetime.

A qualification invocation using the longer scratch suffix `full-final` hit the
existing Linux Unix-socket path-length guard in legacy fixtures. The final
invocation uses short scratch directory `f2`; no product path limits were relaxed.

## Final results

- Full default-profile harness: **85 passed**, zero failed/skipped/infrastructure
  errors. This includes the Basic scope and exact registrations for all 13 new
  recursive unit methods. Receipt: `qualified.json`.
- Targeted real-mount harness: **4 passed**, zero failed/skipped/infrastructure
  errors. Receipt: `hosts.json`.
- Final installed-package recursive host run: all assertions passed, including
  offline restart, nested protection, pin isolation and pre-mount refusal.
  Receipt: `recursive-host-receipt.json`.
- Test-owned mounts are absent after completion; host stop responses assert no
  remaining owned children. `git diff --check` passes.
- Local qualification artifacts are under `~/.scratch/gvs-rec-20261027/`;
  generated harness HTML was moved out of the repository.
