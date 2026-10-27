# Staging optimization — 2026-10-27

## Measurements and decision

Disposable Git 2.43 repositories on the Azure host's `/mnt` ext4 volume; no
production service or source data used. Warm-cache synthetic small JSON files;
cache state uncontrolled. Two runs per completed method, reversed order on the
second run. Slow baselines were deliberately terminated at the documented limit.

| Index entries | Changed paths | Original literal add | Directory-root add | Exact update-index |
|---|---:|---:|---:|---:|
| 150,000 | 10 | 0.10s | 0.09s | 0.07s |
| 150,000 | 1,000 | 1.38s | 0.10–0.13s | 0.09s |
| 150,000 | 140,000 | exceeded 45s | 2.43–2.61s | 1.86–1.87s |
| 500,000 | 1,000 | 4.50–4.57s | 0.33–0.34s | 0.25s |
| 500,000 | 10,000 | 43.06–43.50s | 0.62–0.63s | 0.47–0.49s |
| 500,000 | 140,000 | exceeded 90s | 4.62–4.85s | 3.56–3.58s |

All completed methods produced identical trees. The actual implementation,
including index copy, authorization and write-tree, took **5.37 / 5.38s** for
500,000 entries / 140,000 changes. This is not a prediction of production timing.
The report's production sample was not benchmarked or modified.

Trace2 shows the 10,000-path literal run spending ~4.28s in index preload and
~38.1s between directory traversal and index write, while index read/write total
~0.22s. The 140,000-change directory-root run spends ~0.11s in preload and ~4.28s
between traversal and index write. This supports pathspec-related work as the
bottleneck, without asserting an exact internal complexity from these traces.

Native git-add was chosen over direct index plumbing to preserve established Git
semantics at a small measured cost. No new persistent cache or data schema.
The manifest and its digest remain unchanged. Expanded execution roots cannot
broaden authorization: the alternate index's complete delta is checked before
installation. A deleted ancestor blob is permitted only for an authorized added
child, matching Git's file-to-directory replacement behavior.

The single-common-root probe took 36.48s. A dispersed 257-path literal probe took
2.03s, so sparse modest manifests keep the original method. Root compaction is
used only with at least fourfold reduction and at most 256 roots; common-root
fallback applies above 10,000 manifest entries. These are internal conservative
heuristics, not a claim of optimality for every repository. Broad scans may still
cost time proportional to repository size; large file hashing remains necessary.

## Review

Independent plan review found the sparse-update broad-root regression and
recommended moving reset into the alternate index. Both were addressed before
implementation. Independent code review found a lock-release race: cleanup after
installation could remove a subsequent writer's nested lock. Cleanup now happens
only while the main lock is still owned, with a deterministic regression test.
The reviewer reported no remaining blocking issues and judged complexity
proportionate. Both reviews explicitly considered overengineering, performance
regressions, and excessive rare-case handling.

## Evidence notes

Raw result files and representative Trace2 logs are adjacent to this document.
`children_peak_rss_kib` in the experiment is the cumulative child maximum from
getrusage, not per-command RSS. The zero-change command probes measure Git command
overhead; the actual unchanged-manifest path skips add. No cold-cache or whole
view-advance performance claim is made. Full operation timing also includes
sync, completeness checks, commit and restart, outside these staging benchmarks.

## Cleanup

Removed both owned remote fixtures (11.5s and 3.5s), benchmark script and logs.
`/mnt/alexb-scratch` was verified empty, as originally found. Existing warning
file, lost+found, swapfile and scratch-directory inode/size/mode/ownership were
unchanged. Swap remained active, with the same reported usage. No test mount,
daemon or production service was created or restarted on the remote host.

## Validation

Official harness Basic: 40 passed. Full: 108 passed, with the sole host case
initially failing before staging because `/tmp` was tmpfs; rerunning that exact
case on an ext4 scratch directory passed. Thus all 109 Full cases passed across
those runs. An earlier inventory failure was corrected by registering each new
test method explicitly. Final Basic also passed the inventory checks.
Six focused regressions cover compact staging, authorization, restaging, locks,
replacement semantics and execution-root selection. Existing fresh/resumed
publication and real mounted submodule publication tests passed. No production
runtime was changed. Code is delivered on `feature/efficient-staging`.
