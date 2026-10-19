# GitViewspaceFS and OverlayFS Feasibility and Format-Freeze Study

Status: completed research specification

Related runtime architecture: [`git-viewspace`](../../../spec/git-viewspace-architecture.md)

This study was the original Stage 0 feasibility gate. It proves the ordinary workload path
through GitViewspaceFS and OverlayFS and freezes the naturally generated artifact
forms needed by later replay. It is deliberately not an exhaustive filesystem
conformance, corruption, security, or durability campaign.

If the focused stack cannot meet a mandatory gate, stop and report the blocker
before implementing production replay.

## 1. Objectives

Stage 0 answers these questions:

1. Can one persistent GitViewspaceFS process lazily expose configured commits with
   the ownership and modes needed by an ordinary workload through OverlayFS?
2. Do representative creates, copy-ups, edits, deletions, replacements, and
   renames produce a small, unambiguous set of persistent ext4 upper forms,
   while leaving OverlayFS's private workdir opaque?
3. Does a redirect-bearing upper survive clean unmount/remount and replacement
   of GitViewspaceFS with an equivalent process exposing the same immutable
   alias/commit?
4. Are simultaneous views isolated, and does view startup remain structurally
   lazy rather than walking the selected Git tree?

The study includes the minimum GitViewspaceFS changes needed for stable workload
UID/GID, Git-compatible lower modes, fixed FUSE options, and a root-identity
filesystem-source token. It does not implement the `git-viewspace` daemon, view
state machine, replay engine, or commit path.

Actual host reboot and sudden-power-loss testing are outside Stage 0.

## 2. Privilege boundary

Every filesystem operation whose behavior is under test runs through the
merged view as the configured ordinary, non-root workload UID/GID.

Read-only privileged operations are allowed when necessary. Stage 0 uses this
allowance only for passive inspection of mount state and complete stopped raw
upper/work data, including kernel-created `trusted.overlay.*` xattrs and
whiteout metadata. Every such read must be bounded to validated experiment
paths and retained in the privilege audit.

Privileged operations that modify host state are limited to establishing and
removing the intended FUSE and OverlayFS mounts, including the foreground
GitViewspaceFS process that owns the FUSE mount. No other state-changing privileged
operation is allowed unless a new blocker is reported and explicitly resolved.

Root must not create or alter a scenario entry, change its ownership, create a
device, set a privileged xattr or inode flag, fabricate/corrupt a raw upper
fixture, or delete retained artifacts. If producing the state being tested
requires elevated privilege, the test is out of scope.

All non-mount directories, permissions, repository fixtures, and ordinary test
entries are prepared without elevated privilege. Kernel-created privileged
OverlayFS metadata resulting from an ordinary workload operation remains valid
passive evidence.

## 3. Supported environment and fixed profiles

The experiment requires:

- a Linux host with `/dev/fuse`, OverlayFS, and an isolated ext4 test area;
- the GitViewspaceFS fork in this repository and a clean Python runtime;
- a fresh ordinary, local, non-bare SHA-1 Git repository;
- one configured ordinary workload UID/GID; and
- failure-safe mount/process cleanup with bounded waits.

For the initial VPS experiment, `/path/to/local-ext4` is the candidate ext4 area.
The harness must probe and record the actual backing filesystem and relevant
features rather than trust the pathname.

### 3.1 GitViewspaceFS/FUSE profile

- read-only FUSE mount;
- `allow_other`;
- `default_permissions`;
- `negative_timeout=0`;
- foreground execution under controller privilege;
- stable reported ownership equal to the workload UID/GID;
- regular files `0644`, executable files `0755`, and directories `0755`;
- normal Linux symlink semantics; and
- a filesystem-source token containing the experiment root UUID and unique
  process-attempt token.

### 3.2 OverlayFS/ext4 profile

- `redirect_dir=on`;
- `metacopy=off`;
- `index=off`;
- `nfs_export=off`;
- `xino=off`;
- `uuid=null`;
- `verity=off`;
- no `volatile` or `userxattr` mode;
- one pinned GitViewspaceFS alias as `lowerdir`;
- dedicated, non-overlapping `upperdir` and `workdir` on the same ext4
  superblock; and
- one dedicated merged mountpoint.

`uuid=null` is required because the GitViewspaceFS lower and ext4 upper are
different filesystems. Linux 6.12 cannot honor `uuid=off` for that topology
and falls back to `uuid=null`; Stage 0 must request and observe the supported
value directly rather than accepting a fallback. Requalify this choice if
index, NFS export, metacopy, xino, or the lower-filesystem type changes.

Record the kernel release and machine, the workspace filesystem
type/source/UUID/root/options from ordinary `findmnt` and
`/proc/self/mountinfo`, the readable relevant OverlayFS module parameters,
workload UID/GID, and GitViewspaceFS/FUSE software versions. For each FUSE and
OverlayFS mount, retain the requested profile and the effective mount facts
reported by mountinfo and GitViewspaceFS startup. The workspace must report ext4,
upper/work must share one filesystem identity, and every requested/effective
profile value must agree; otherwise the gate fails.

Raw block-device or superblock reads, kernel-configuration capture,
kernel-journal collection, live-process/shared-library attestation, and
boot-continuity analysis are outside this focused stage.

## 4. Exact fixture states

The primary valid commit contains:

```text
files/plain              regular 100644, bytes "plain-v1\n"
files/other              regular 100644, bytes "other-v1\n"
files/exec               regular 100755, bytes "#!/bin/sh\nexit 17\n"
links/relative           symlink target "../files/plain"
links/dangling           symlink target "missing-target"
dirs/A/a                 regular 100644, bytes "A-a\n"
dirs/A/sub/b             regular 100644, bytes "A-b\n"
dirs/B/c                 regular 100644, bytes "B-c\n"
deep/l1/l2/l3/leaf       regular 100644, bytes "deep\n"
literal/.wh.visible      regular 100644, bytes "literal-whiteout\n"
literal/.wh..wh..opq     regular 100644, bytes "literal-opacity-name\n"
unicode/<U+00E9>         regular 100644, bytes "unicode\n"
```

Also create:

- a second valid commit with visibly different file, executable, symlink, and
  directory contents; and
- a large valid commit containing at least 10,000 ordinary entries plus the
  same deep path, used only for structural-laziness comparison.

Every isolated scenario starts with a fresh empty `upper/` and `work/` unless
the scenario explicitly continues a named sequence.

## 5. Focused operation catalog

Every scenario has a predeclared canonical ID, exact initial commit/tree,
relative paths, byte payloads, syscall arguments, workload credentials/umask,
expected result, checkpoints, and Git-tree oracle. The registry is frozen
before the first scenario mutation. Runtime discovery may not add a passing
case.

### A. Baseline and lookup

- `A01`: mount, `lstat`, `readdir`, and read one regular, executable,
  directory, relative symlink, and dangling symlink; normal unmount must leave
  no user delta.
- `A02`: look up one missing path and read only `deep/l1/l2/l3/leaf`; prove
  lookup remains demand-driven and creates no copy-up.

The measurement phase runs before any evidence traversal that would warm the
same alias. A separate identical verification run may perform full capture.

### B. Ordinary creation

- `B01`: create a regular file containing text plus NUL bytes, overwrite part
  of it, and truncate it.
- `B02`: create a regular file, add the executable state, then remove it.
- `B03`: create one relative symlink and one dangling symlink.
- `B04`: create a nested directory with regular and executable children;
  separately create and remove an empty upper-only directory.
- `B05`: create one multibyte UTF-8 name and ordinary regular files named
  `.wh.visible` and `.wh..wh..opq`. They must remain distinguishable from
  OverlayFS metadata by inode/xattr form, never by basename alone.

### C. Lower-backed copy-up and modification

- `C01`: overwrite `files/plain` with `plain-v2\n`.
- `C02`: append `tail\n` to `files/plain`, capture, then truncate it to four
  bytes and capture again.
- `C03`: add and then remove the executable state on `files/plain`.
- `C04`: remove `links/relative` and create a regular file at the same path.
- `C05`: perform the common editor-save sequence: create a sibling temporary
  regular containing `editor-v2\n`, `fsync` it, rename it over `files/plain`,
  and `fsync` the parent directory.
- `C06`: as the workload identity, set `user.viewspace-stage0` to the bytes
  `ordinary-xattr\n` on `files/plain`, capture the resulting copy-up, then
  remove that xattr and capture again. The xattr is ordinary metadata ignored
  by the Git-tree oracle, not a privileged or grammar-defining OverlayFS
  marker.

### D. Deletion and representative type replacement

- `D01`: unlink lower-backed `files/plain`; capture the generated whiteout.
- `D02`: create and unlink a pure-upper regular; final semantics equal the
  base and must not contain a lower-entry whiteout.
- `D03`: unlink lower-backed `links/relative`; capture the generated whiteout.
- `D04`: remove the visible contents of lower-backed `dirs/A`, remove the
  directory, recreate it, and add one new child; capture any generated
  whiteout and opacity forms.
- `D05`: at one lower regular path, perform
  `regular -> directory-with-child -> symlink -> regular`, capturing every
  intermediate stopped form.

These representative transitions replace the former exhaustive pairwise type
matrix.

### E. Rename and redirect

- `E01`: rename a pure-upper regular within one parent.
- `E02`: rename a lower-backed regular within one parent.
- `E03`: rename a lower-backed regular across parents.
- `E04`: rename a regular over an existing lower-backed regular destination.
- `E05`: rename lower-backed regular `A -> B -> C`, capturing both steps.
- `E06`: rename a pure-upper populated directory across parents.
- `E07`: rename lower-backed `dirs/A` within one parent; capture the redirect,
  source whiteout, opacity, and any `trusted.overlay.impure` metadata.
- `E08`: rename lower-backed `dirs/A` across parents, then create, modify, and
  delete one child through the destination and rename the directory once more.
- `E09`: attempt a standard rename over a nonempty directory; require the
  normal Linux error and unchanged-state proof.

Only ordinary `renameat2(..., flags=0)` behavior is exercised. There are no
special-flag, swap, cycle, path-limit, or Cartesian rename campaigns.

### F. Remount and process-lifecycle composites

- `F01`: construct one composite upper containing a pure-upper file, regular
  copy-up, executable change, symlink replacement, file whiteout, recreated
  directory/opacity, and lower-directory redirect. Cleanly unmount and remount
  it against the same live GitViewspaceFS alias; merged semantics must be identical.
- `F02`: cleanly unmount the `F01` overlay, stop GitViewspaceFS, start a replacement
  process exposing the identical immutable alias/commit, then remount. This is
  the hard redirect-persistence feasibility gate.
- `F03`: repeat orderly OverlayFS-first stop/start/remount three times using the
  composite state.
- `F04`: run one representative regular-file edit with application `fsync`,
  directory `fsync`, and `syncfs`; verify it introduces no unclassified
  persistent artifact compared with the ordinary clean-unmount grammar.

There is no actual host-reboot scenario.

### G. Access, isolation, and structural laziness

- `G01`: as the workload UID/GID, read lower-backed data and create, modify,
  rename, and delete through the public merged mount. The manager-free Stage 0
  harness does not use elevated ownership changes to test the later
  controller-owned raw-storage permission layout.
- `G02`: compare complete primary and secondary lower manifests before and
  after every mutating catalog scenario and prove they are unchanged. The
  large fixture is defined by its Git-tree fixture manifest but is not walked
  through GitViewspaceFS outside `G04`.
- `G03`: simultaneously mount independent `same-a` and `same-b` overlays over
  the primary commit and a `different` overlay over the secondary commit;
  mutate only `same-a` and prove the other two remain unchanged. Each variant
  retains its own literal `P00` through `P05` cycle. Its `P03` and `P05` upper
  signatures must match, and the untouched variants' `P00` and
  `P05` upper signatures must match.
- `G04`: compare mount/pin traces and I/O counters for the primary commit and
  the 10,000-entry large commit, then demand the same deep path from each.
  Startup must not recursively traverse or materialize the selected tree. Use only
  GitViewspaceFS's own instrumentation/logging and ordinary unprivileged
  observations; do not use `perf`, eBPF, `ptrace`, or privileged process
  attachment. Measure primary first and large second, with a fresh GitViewspaceFS
  process and one OverlayFS mount for each. Retain the distinct process
  boundaries, exact pre-mount/post-mount/post-read counters and deltas, and
  the asserted `deep/l1/l2/l3/leaf` bytes (`deep\n`) for both. Each delta must
  remain within the fixed per-demand bounds, and the primary/large totals for
  the pin and read phases may differ by at most 32 events. `G04` performs no
  checkpoint, complete-tree walk, Git-tree oracle export, remount, or raw
  upper/work capture. It runs last, after the final primary/secondary lower
  immutability capture, so its second fresh process may stop without another
  mounted-tree walk.

## 6. Capture cycle and evidence

Each isolated scenario except the deliberately measurement-only `G04` uses
this bounded cycle:

1. `P00-before-mount`: capture lower identity plus pristine stopped upper,
   work, and bare mountpoint.
2. `P01-mounted`: record mount facts before the operation. Observer-sensitive
   scenarios suppress a full tree walk until measurement finishes.
3. `P02-after-step`: record the exact operation result/errno and merged state
   after every named step.
4. `P03-after-unmount`: normally unmount and passively capture complete stopped
   lower, upper, work, and bare mountpoint state.
5. `P04-after-remount`: remount the identical alias/profile and compare merged
   semantics with the oracle.
6. `P05-final-unmount`: normally unmount and verify no remount-induced change
   to the persistent upper semantics. Workdir internals are captured but may be
   consumed or rewritten by OverlayFS during a successful remount.

Whenever a catalog sequence says to capture an intermediate artifact state,
that `P02` is followed immediately by a normal unmount, stopped raw upper/work
capture, identical remount, and semantic check before the next operation. Thus
executable-on/off, append/truncate, type-transition, chained-rename, and
redirect-child-edit states each become observed stopped forms rather than only
live merged observations.

Raw upper/work are never traversed while OverlayFS is mounted. Live checkpoints
record them as unavailable live layers.

For every stopped entry, retain:

- scenario/checkpoint/layer and exact relative UTF-8 path bytes;
- `lstat`/`statx` type, complete mode, UID/GID, inode, link count, size, blocks,
  mount/filesystem identity, timestamps, and device number where applicable;
- every no-follow xattr name and value visible to the passive controller
  capture, including kernel-created `trusted.overlay.*`;
- regular-file bytes, length, and SHA-256;
- no-follow symlink target bytes;
- complete directory child-name set; and
- lookup/read/readlink/expected-absence observations through the live mount.

Capture the concise run profile before the first scenario. A retained scenario
pass may be reused only while its fixture/config identity, kernel release,
workspace filesystem identity/options, relevant OverlayFS parameters, workload
identity, and fixed mount profiles still match. Stage 0 has no boot-ID or
journal-cursor continuity protocol; reboot testing remains excluded.

For each GitViewspaceFS start, retain its command, unique attempt token,
filesystem-source token, root UUID, config alias-to-commit identity, version,
and log reference. Replacement processes use the same root UUID, config, and
mount profile with a fresh attempt token.

The bundle also retains exact scenario operations, credentials, mountinfo,
requested/effective mount facts, the concise ordinary-user environment record,
GitViewspaceFS config/logs, and the targeted `G04` traces/counters. At stopped
checkpoints, the fixed bounded read-only privileged reader retains complete raw
upper/work transcripts, their materialized records, and matching audit entries.
No kernel-journal evidence is required.

## 7. Evidence bundle

The execution produces one versioned, checksummed bundle:

```text
stage-0-evidence/
+-- manifest.json
+-- environment.json
+-- scenario-registry.json
+-- results.jsonl
+-- fixture-manifest.json
+-- artifact-format.md
+-- artifact-format.json
+-- schemas/
+-- blobs/sha256/
+-- runtime/
    +-- run-fingerprint.json
    +-- processes.json
    +-- sudo-invocations.json
+-- scenarios/<scenario-id>/
    +-- scenario.json
    +-- attempts/<attempt>/
        +-- operations.jsonl
        +-- laziness-measurements.json (G04)
        +-- checkpoints/<checkpoint>/ (all ordinary scenarios except G04)
            +-- checkpoint.json
            +-- mountinfo.txt
            +-- lower.jsonl
            +-- upper.jsonl
            +-- upper.privileged.jsonl (stopped checkpoints only)
            +-- work.jsonl
            +-- work.privileged.jsonl (stopped checkpoints only)
            +-- merged.jsonl
            +-- expected-git-tree.jsonl
        +-- variants/<variant>/checkpoints/<checkpoint>/
            +-- the same checkpoint files, used by G03
```

Every scenario ID has exactly one final `pass`, `fail`, or blocker-linked
`not_run` result. A successful gate requires every scenario to pass. Byte
content is stored losslessly; there is no lossy pathname or target conversion.

Every stopped upper/work summary corresponds to exactly one privileged-reader
transcript and audit record, and every privileged-reader audit corresponds to
exactly one stopped layer. Every remaining sudo audit corresponds to one
retained FUSE/OverlayFS mount, process-start, or unmount transition. Missing,
duplicate, malformed, or orphaned evidence blocks the gate.

The retained bundle must be sufficient to reproduce the focused semantic
fixtures used to implement and test `view sync`. It need not reconstruct unsupported device nodes,
privileged metadata, or deliberately corrupted raw uppers.

## 8. Artifact-format freeze

The durable, detailed result of this freeze is
[Qualified OverlayFS Upper Artifact Format](../../../spec/OverlayFS-artifact-format.md).
The generated local `artifact-format.json` remains the byte-level occurrence
record; the tracked document preserves the format and replay implications for
`view sync`.

`artifact-format.md` and `artifact-format.json` record only persistent forms
naturally generated by the focused ordinary-workload catalog. They must define:

1. pristine stopped upper/work root state;
2. accepted ordinary regular, executable, symlink, and directory forms;
3. the exact kernel-generated whiteout representation, root ownership, and placement;
4. exact opacity xattr values and semantics;
5. exact redirect xattr bytes, anchoring, source whiteout, behavior when the
   redirect-bearing directory is renamed again in `E08`, and replay
   precedence;
6. exact generated `trusted.overlay.impure` placement/value and any other
   kernel metadata actually observed;
7. ordinary `user.*` xattrs and POSIX ACL xattrs as permitted-but-ignored
   metadata classes whose names/values never affect the Git-tree oracle;
   private `trusted.overlay.*` metadata remains grammar-significant and all
   other unrecognized xattr namespaces are rejected;
8. the observed stopped workdir states, explicitly classified as opaque
   kernel-private data that is recorded but never interpreted or replayed;
9. fields that are grammar-significant, ignored, or permitted to vary; and
10. the concise qualifying run profile: evidence/harness version, kernel
    release/machine, workspace ext4 identity/options, fixed requested/effective
    FUSE and OverlayFS profiles, readable relevant OverlayFS module parameters,
    GitViewspaceFS/FUSE versions, and workload UID/GID.

`artifact-format.json` identifies `runtime/run-fingerprint.json`, and the final
checksum manifest binds both files. This is a feasibility-profile binding, not
kernel, block-device, process, or shared-library forensic attestation.

The positive artifact grammar is closed. The replay implementation accepts only these
exact kernel-generated forms plus the explicitly named ignored metadata
classes, and rejects unknown inode/xattr combinations by default. The study does
not fabricate malformed examples to prove every rejection branch; `view sync` may
test its parser with ordinary in-memory fixtures without privileged
filesystem scaffolding.

## 9. Git-tree oracle

Every positive scenario has an independently generated expected Git tree
containing only:

- exact regular-file bytes;
- executable versus non-executable Git mode;
- exact valid-UTF-8 symlink target bytes;
- directory structure induced by surviving Git entries; and
- absence caused by deletion, whiteout, or opacity.

Ownership, timestamps, ACLs, ordinary xattrs such as the one exercised by
`C06`, non-executable permission bits, allocation details, and empty
directories do not enter the Git oracle.

The expected oracle and live/remounted merged-view semantics must agree for
every supported scenario. Every stopped-upper artifact must be classified and
linked unambiguously to that oracle delta. The study did not reconstruct a
checkout by replaying the raw upper; that behavior belongs to `view sync`.

## 10. Explicit exclusions

Stage 0 does not test:

- hard links;
- actual host reboot or sudden power loss;
- FIFO, Unix socket, block/character device, capability, ACL, immutable flag,
  privileged ownership, or other unsupported special/privileged states;
- setting/removing `trusted.overlay.*` by the harness;
- fabricated or deliberately malformed raw OverlayFS upper/work state;
- exhaustive pairwise type transitions or rename Cartesian products;
- special `renameat2` flags, swap/cycle combinatorics, or path-limit sweeps;
- sparse allocation/hole-punch behavior; or
- exhaustive invalid-encoding, Git-object, or filesystem-boundary cases.

These exclusions do not turn an unobserved kernel artifact into a supported
one. The sole policy-level exception is the explicitly named
permitted-but-ignored POSIX ACL xattr class in Section 8; it is not an
OverlayFS format form and is intentionally not exercised by Stage 0. The
replay layer remains conservative and rejects anything else outside the frozen
positive grammar.

## 11. Exit gate

Stage 0 passes only when:

- the requested FUSE/OverlayFS profile is effective;
- every focused ordinary-workload operation succeeds, and `E09` fails without
  mutation as expected;
- the lower remains unchanged;
- every naturally generated persistent upper form is classified and maps
  unambiguously to the Git oracle, while stopped workdir data is captured
  losslessly and treated as opaque;
- the composite state survives clean remount, three process-lifecycle cycles,
  and GitViewspaceFS replacement with the same alias/commit, including redirects;
- same-commit and different-commit views remain isolated;
- ordinary-workload access through the merged mount behaves as specified;
- structural laziness is demonstrated; and
- the concise run profile is complete and agrees with every effective mount;
- the privilege audit and retained mount/passive-read evidence agree
  bidirectionally; and
- the evidence bundle and format freeze pass adversarial review.

Any unexplained persistent artifact, lower mutation, ambiguous Git mapping,
eager recursive tree walk, isolation failure, or redirect-bearing remount
failure blocks progression to production replay.

## 12. Primary references

- [Linux OverlayFS documentation](https://docs.kernel.org/filesystems/overlayfs.html)
- [Kernel FUSE documentation](https://www.kernel.org/doc/html/latest/filesystems/fuse/fuse.html)
- [libfuse configuration reference](https://libfuse.github.io/doxygen/structfuse__config.html)
- [Git tree-mode documentation](https://git-scm.com/docs/git-fast-import)
