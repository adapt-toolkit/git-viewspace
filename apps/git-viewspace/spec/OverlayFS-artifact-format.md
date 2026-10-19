# Qualified OverlayFS Upper Artifact Format

Status: qualified format contract for the `git-viewspace` MVP

This document is the durable, human-readable account of the raw OverlayFS
upperdir forms observed by Stage 0. It defines the input contract for `view
sync` without depending on an ignored
local evidence directory.

It records two different things and labels them separately:

1. **Observed facts** are exact results from the canonical Stage 0 run.
2. **MVP policy** says how the replay classifier must validate or interpret
   those facts.

Fixture-specific occurrence paths prove what the tests actually produced;
they are not a production rule limiting artifacts to those literal names.
General classifier rules must be structural and fail closed.

The completed study specification, exact scenario registry, machine-readable grammar, fixture
contract, qualification profile, and complete result summary are retained in the
[archived OverlayFS study](../research/OverlayFSStudy/README.md) as a compact evidence capsule. The
historical executable harness is not retained.

## 1. Evidence authority

The source run passed all 35 Stage 0 scenarios.

| Field | Value |
| --- | --- |
| Run UUID | `956a13f5-26e8-4ebb-8925-5783594d79aa` |
| Evidence format | `viewspace-stage0-evidence-v2` |
| Harness version | `2` |
| Evidence manifest SHA-256 | `ecd14695ea35ad76cc90331def5b22e96b4c4fd25851d07fd90555588b23aa21` |
| Run-fingerprint SHA-256 | `f4a2f3343f4488bb46e88e91faa017cb9ad2091cdc8fccd038b783e4edcbb95e` |
| Generated `artifact-format.json` SHA-256 | `6d2a9a7ea011eb6ce2eac9f4984f2737f937dd425ad9167864cae61b9e7b68a0` |
| Generated `artifact-format.md` SHA-256 | `766e2bcbeefeb7fbe70da7fa697445283fc474ffa1987a60d1abd3c7c811d387` |
| Scenario outcome | 35 passed, 0 failed, 0 omitted |
| Manifest coverage | 3,936 files, with no missing, extra, or mismatched file |
| Persistent upper inventory | 29 distinct forms, 684 retained entry occurrences |
| Trusted-xattr occurrences | `impure` 228, `opaque` 28, `origin` 264, `redirect` 28 |
| Whiteout occurrences | 74 |
| Stopped work inventory | 194 captured states |

The retained local source was:

```text
.stage0-work/canonical-v2-r5/stage-0-evidence/
```

That directory is intentionally ignored by Git. The digest above identifies
the exact source bundle from which this document was extracted. This document
contains the facts needed for design; the ignored bundle remains the
byte-level audit record on the qualifying host.

No new experiment was performed to produce this document.

## 2. Qualified profile

These forms are qualified only for this profile. A different effective
profile requires a new feasibility run or an explicit compatibility decision.

### 2.1 Host and storage

| Property | Qualified value |
| --- | --- |
| OS | Debian GNU/Linux 13 (trixie) |
| Kernel | `6.12.96+deb13-cloud-amd64` |
| Machine | `x86_64` |
| Upper/work filesystem | ext4, `/dev/sda1`, major:minor `8:1` |
| ext4 UUID | `a04243ec-b7c4-4a25-bc95-2d1dc1c1b8e5` |
| ext4 mount options | `rw,relatime,discard,errors=remount-ro` |
| Filesystem limits observed | block size 4096, `NAME_MAX` 255 |
| Workload identity | UID 1000, GID 1003 |
| Workload umask | `0022` |

Upper and work were ordinary directories on the same ext4 filesystem. The
lower was the read-only git-viewspace-fs FUSE mount, so lower and upper necessarily
belonged to different filesystems.

### 2.2 git-viewspace-fs/FUSE profile

| Property | Qualified value |
| --- | --- |
| git-viewspace-fs | `0.2.6+gitviewspacefs.3` |
| Python | 3.13.5 |
| fusepy | 3.0.1 |
| libfuse soname | `libfuse.so.2` |
| pygit2 | 1.20.0 |
| libgit2 | 1.9.6 |
| FUSE behavior | read-only, `allow_other`, `default_permissions`, `negative_timeout=0` |
| Reported lower ownership | UID 1000, GID 1003 |
| Reported lower modes | directory 0755, regular 0644/0755, symlink 0777 |

git-viewspace-fs was launched as root solely to establish the FUSE mount, so
mountinfo reported FUSE `user_id=0,group_id=0`. Those fields identify the
FUSE process owner; they are distinct from the stable 1000:1003 ownership
reported for exposed lower entries and observed on ordinary upper entries.

### 2.3 OverlayFS profile

Every tested mount requested the following fixed profile:

```text
redirect_dir=on
metacopy=off
index=off
nfs_export=off
xino=off
uuid=null
verity=off
```

The relevant readable module parameters were:

| Parameter | Value |
| --- | --- |
| `check_copy_up` | `N` |
| `index` | `N` |
| `metacopy` | `N` |
| `nfs_export` | `N` |
| `redirect_always_follow` | `Y` |
| `redirect_dir` default | `N` |
| `redirect_max` | `256` |
| `xino_auto` | `N` |

The per-mount `redirect_dir=on` option deliberately overrides the module's
default for these mounts. `uuid=null` is the supported effective mode when the
FUSE lower and ext4 upper are different filesystems.

Mountinfo directly reported `redirect_dir=on` and `uuid=null`. It omitted the
requested `metacopy=off`, `index=off`, `nfs_export=off`, and `xino=off`
spellings; the captured module defaults for those features were all `N`.
`verity=off` was requested and accepted but was likewise not printed. No
mount used `volatile` or `userxattr`. Consequently the qualification is the
combination of requested options, reported effective options, and recorded
module defaults—not a claim that every requested word appeared in mountinfo.

## 3. Capture model

Raw upper and work trees were inspected only while OverlayFS was unmounted.
The controller ran a bounded, read-only privileged reader so that
`trusted.overlay.*` values and root-owned whiteouts were visible. The reader
captured entries without following symlinks and retained:

- exact relative path bytes and UTF-8 decoding;
- inode type and complete mode;
- UID, GID, link count, device number, inode number, size and allocation data;
- timestamps and statx fields;
- every no-follow xattr name and value;
- complete regular-file bytes and SHA-256;
- exact symlink target bytes; and
- exact directory child-name sets.

The normal evidence cycle was:

| Checkpoint | Meaning |
| --- | --- |
| `P00-before-mount` | pristine stopped upper/work |
| `P01-mounted` | mounted state before the operation |
| `P02-step-NNN-live` | merged state immediately after a named operation |
| `P02-step-NNN-stopped` | raw state after unmounting at that step |
| `P02-step-NNN-remounted` | merged state after remounting the same upper/work |
| `P02-step-NNN-remount-stopped` | raw state after that verification remount |
| `P03-after-unmount` | final stopped raw state |
| `P04-after-remount` | final merged semantic verification |
| `P05-final-unmount` | final stopped state after verification remount |

The future sync implementation must likewise inspect only a stopped upper.
Live upperdir traversal is outside the contract.

## 4. Path and ordinary-entry grammar

### 4.1 Paths

MVP policy requires every replay-visible path component to be valid UTF-8 and
host-representable. Within an upper inventory:

- paths are relative to the upper root; `.` denotes that root;
- empty components, `.` components, `..` components, embedded slash and NUL
  are invalid;
- no lossy decoding or Unicode normalization is performed; and
- directory relationships and exact component bytes are significant.

### 4.2 Ordinary entries

| Entry | Observed Stage 0 form | Git-semantic interpretation |
| --- | --- | --- |
| Directory | UID:GID 1000:1003, mode 0755, no special bits | Defines tree structure. Empty directories disappear from the Git tree. Replay normalizes surviving directories to 0755. |
| Non-executable regular | UID:GID 1000:1003, one link, mode 0644 | Exact bytes become Git mode 100644. |
| Executable regular | UID:GID 1000:1003, one link, mode 0755 | Exact bytes become Git mode 100755. |
| Symlink | UID:GID 1000:1003, mode 0777 | Exact valid-UTF-8 target becomes Git mode 120000. The target is not normalized. |

For MVP classification, the execute/non-execute class matters; other ordinary
permission detail does not. Setuid, setgid, sticky regular files, multiply
linked regular files and unsupported inode types are rejected.

Stage 0 explicitly created ordinary regular files named `.wh.visible` and
`.wh..wh..opq`. They remained ordinary files. An OCI-style basename is never
sufficient to classify a kernel OverlayFS whiteout or opaque directory.

## 5. Field disposition

### 5.1 Significant fields

The sync result depends on:

- exact UTF-8 path and tree position;
- inode type;
- exact regular-file bytes;
- regular executable versus non-executable class;
- exact symlink target bytes;
- exact whiteout type, device, mode and placement; and
- the name, value, combination and structural placement of recognized
  `trusted.overlay.*` metadata.

### 5.2 Recorded but Git-ignored fields

The following were retained as evidence but do not affect the resulting Git
tree:

- configured ordinary UID/GID values;
- atime, mtime, ctime and birth time;
- inode and device numbers;
- allocation size, block count, block size and statx attributes;
- directory link count;
- ordinary non-executable permission detail;
- accepted ordinary xattrs and ACL xattrs; and
- empty directories.

The controller identity and expected ordinary ownership remain deployment
validation facts even though Git ignores ownership.

### 5.3 Structurally constrained variable field

The final eight bytes of `trusted.overlay.origin` varied across lower objects.
They are validated as a nonzero opaque handle tail but are not treated as a
stable identity or Git-semantic value.

## 6. Kernel-generated whiteout

### 6.1 Exact form

A qualified whiteout is not a `.wh.*` filename. It is exactly:

| Field | Required value |
| --- | --- |
| Inode type | character device |
| Complete mode | `020000` (`S_IFCHR` plus permission bits 0000) |
| Device number | 0:0 (`rdev=0`) |
| UID:GID | 0:0 |
| Permission bits | 0000 |
| Link count | 1 or 2 in the observed records; not a recognition field |
| Xattrs | none |
| Contents/target | none |

Any character device that does not match this entire form is unsupported.
A whiteout with any xattr is unsupported.

The kernel sometimes reported link count 2 in an initial stopped capture and
1 later. Size, allocation, inode number, timestamps and link count are
recorded diagnostics, not part of whiteout recognition. Type, complete mode,
ownership, device number, xattr emptiness and layer placement are decisive.

### 6.2 Meaning

At path `p`, the whiteout suppresses the lower entry at `p`. It contributes no
Git entry itself. Under a redirect-bearing directory it suppresses the
corresponding child in the redirected lower directory.

### 6.3 Final observed placements

The 74 retained whiteout occurrence records include stopped/remount-stopped
and P03/P05 repetitions. There were 15 whiteouts at the final P03 checkpoints:

| Scenario | P03 whiteout path(s) |
| --- | --- |
| `D01` | `files/plain` |
| `D03` | `links/relative` |
| `E02` | `files/plain` |
| `E03` | `files/plain` |
| `E04` | `files/plain` |
| `E05` | `files/plain` |
| `E07` | `dirs/A` |
| `E08` | `dirs/A`; `dirs/B/moved-A-again/sub/b` |
| `F01` | `dirs/B`; `files/exec` |
| `F02` | `dirs/B`; `files/exec` |
| `F03` | `dirs/B`; `files/exec` |

Two transition details matter:

- In `D04`, removal of lower `dirs/A` produced a whiteout at step 4. Recreating
  that path replaced the whiteout with an ordinary upper directory carrying
  `trusted.overlay.opaque=y`.
- In `E08`, `dirs/A` remained whiteouted after its redirected tree moved.
  Deleting `sub/b` created a second whiteout under `moved-A`; the second
  directory rename moved that child whiteout to
  `dirs/B/moved-A-again/sub/b`.

## 7. `trusted.overlay.opaque`

### 7.1 Exact form and meaning

The observed opacity marker was:

```text
name  = trusted.overlay.opaque
value = y                  # one byte, hexadecimal 79, Base64 eQ==
inode = directory only
```

For sync, an opaque upper directory contributes its own ordinary upper
children but hides all otherwise-visible children from the corresponding
lower directory.

No other value was observed or qualified. No opaque marker combined with an
origin, impure or redirect marker in the observed positive forms.

### 7.2 Occurrences

There were 28 retained occurrence records and six final P03 occurrences:

| Scenario | Path | Lifetime/interpretation |
| --- | --- | --- |
| `B04` | `created-tree` | Present from the captured populated-tree state through P05. |
| `D04` | `dirs/A` | Appeared after the removed lower directory was recreated; present through P05. |
| `D05` | `files/plain` | Intermediate directory-over-lower-regular state only; gone after later replacement by symlink and regular. |
| `E06` | `dirs/B/upper-dir` | Present after the pure-upper populated directory moved into a lower-backed parent; present through P05. |
| `F01` | `dirs/A` | Composite recreated directory; present through P05. |
| `F02` | `dirs/A` | Same composite across git-viewspace-fs replacement. |
| `F03` | `dirs/A` | Same composite across three lifecycle cycles. |

The `B04` result is evidence, not an assertion that every new upper-only
directory must carry opacity.

## 8. `trusted.overlay.redirect`

### 8.1 Exact syntax

The marker appeared only on directories and always together with
`trusted.overlay.origin`.

```text
name = trusted.overlay.redirect
```

The value is strict UTF-8 in one of two forms:

- a canonical relative path, resolved from the redirecting directory's
  containing lower directory; or
- one leading `/` followed by a canonical path, resolved from the overlay
  root.

Empty values, `//`, empty components, `.` and `..` components are not in the
qualified grammar.

### 8.2 Exact observed values and placements

There were three distinct values, 28 retained occurrence records and five
final P03 occurrences:

| Scenario | Upper directory | Raw value | Anchor | Source whiteout |
| --- | --- | --- | --- | --- |
| `E07` | `dirs/A-moved` | `A` (`QQ==`) | containing lower directory, therefore `dirs/A` | `dirs/A` |
| `E08` | `dirs/B/moved-A-again` | `/dirs/A` (`L2RpcnMvQQ==`) | overlay root | `dirs/A` |
| `F01` | `redirected-B` | `/dirs/B` (`L2RpcnMvQg==`) | overlay root | `dirs/B` |
| `F02` | `redirected-B` | `/dirs/B` (`L2RpcnMvQg==`) | overlay root | `dirs/B` |
| `F03` | `redirected-B` | `/dirs/B` (`L2RpcnMvQg==`) | overlay root | `dirs/B` |

In `E08`, the first rename created `moved-A` with `/dirs/A`. The second rename
moved that same redirect-bearing directory to `dirs/B/moved-A-again`; the
value remained `/dirs/A`. The test did not produce a chained redirect value.

### 8.3 Merge precedence

For a redirect-bearing directory, the logical starting children come from
the redirect target in the immutable lower tree. Then:

1. an opaque marker, if a future qualified form permits the combination,
   would hide all lower children;
2. whiteouts suppress matching lower children;
3. ordinary upper children replace matching lower children; and
4. remaining lower children stay visible.

No `opaque+redirect` combination was observed. It is therefore rejected by
the current closed MVP grammar rather than inferred from the hypothetical
precedence above.

## 9. `trusted.overlay.origin`

### 9.1 Observed objects

Origin occurred on copied-up regular files and directories, including
redirect-bearing directories. It was not observed on symlinks or whiteouts.
Fifteen distinct complete values were captured.

### 9.2 Exact 33-byte wire form

All values had this layout:

| Offsets | Length | Meaning | Qualified value |
| --- | ---: | --- | --- |
| 0 | 1 | version | `00` |
| 1 | 1 | magic | `fb` |
| 2 | 1 | encoded length | `21` hexadecimal = 33 bytes |
| 3 | 1 | flags | `00` |
| 4 | 1 | file-handle type | `81` |
| 5..20 | 16 | underlying filesystem UUID field | all zero |
| 21..24 | 4 | first part of 12-byte file handle | all zero |
| 25..32 | 8 | opaque file-handle tail | variable and nonzero |

Thus the fixed 25-byte prefix is:

```text
00fb2100810000000000000000000000000000000000000000
```

The 15 observed variable tails, shown as eight-byte hexadecimal samples,
were:

```text
0d00000000000000  0e00000000000000  1000000000000000
1100000000000000  1300000000000000  1400000000000000
1500000000000000  1600000000000000  1700000000000000
1800000000000000  1900000000000000  1c00000000000000
1d00000000000000  2100000000000000  2200000000000000
```

This list records the run; it is not an allowlist. Across the independently
built but logically equivalent `F01`–`F03` composites, `F03` used different
tails from `F01`/`F02`; within a retained upper the values stayed stable across
clean remounts. The entire layout is an empirical, kernel/profile-bound format
rather than a stable OverlayFS ABI.

The complete file-handle field is 12 bytes, offsets 21 through 32. Calling the
last eight bytes the whole file handle would be incorrect; they are only its
variable opaque tail.

### 9.3 Classifier disposition

The classifier must validate the exact length and fixed fields, require a
nonzero eight-byte tail, and preserve the captured bytes in evidence. It must
not use the opaque tail as a Git object identity, repository path identity or
cross-run stable value.

For sync into a checkout, origin itself makes no Git-tree change. The ordinary
entry carrying it supplies the content, type and executable class. If a future
operation reproduces an upper for remount rather than syncing it to Git, the
origin bytes must remain untouched.

## 10. `trusted.overlay.impure`

### 10.1 Exact form and meaning

The marker occurred only on directories:

```text
name  = trusted.overlay.impure
value = y                  # one byte, hexadecimal 79, Base64 eQ==
```

It is OverlayFS directory-merge bookkeeping. After structural validation it
does not independently add, remove or modify a Git entry. The sync classifier
must not copy it into the checkout.

### 10.2 Exact final P03 occurrence map

| Scenario | Paths carrying `impure=y` |
| --- | --- |
| `C01` | `.`, `files` |
| `C02` | `.`, `files` |
| `C03` | `.`, `files` |
| `C04` | `.` |
| `C05` | `.` |
| `C06` | `.`, `files` |
| `D01` | `.` |
| `D03` | `.` |
| `D04` | `.`, `dirs` |
| `D05` | `.` |
| `E02` | `.`, `files` |
| `E03` | `.`, `dirs`, `dirs/B`, `files` |
| `E04` | `.`, `files` |
| `E05` | `.`, `dirs`, `dirs/B`, `files` |
| `E06` | `.`, `dirs` |
| `E07` | `.`, `dirs` |
| `E08` | `.`, `dirs`, `dirs/B`, `dirs/B/moved-A-again` |
| `F01` | `.`, `dirs`, `files` |
| `F02` | `.`, `dirs`, `files` |
| `F03` | `.`, `dirs`, `files` |
| `F04` | `.`, `files` |

This table is provenance for the tested fixtures. Production validation is
structural: correct directory type, exact value and a permitted observed xattr
combination. Production paths are not required to equal these fixture names.

## 11. Origin occurrence provenance

The exact final P03 paths carrying origin were:

| Scenario | Paths carrying `trusted.overlay.origin` |
| --- | --- |
| `C01` | `files`, `files/plain` |
| `C02` | `files`, `files/plain` |
| `C03` | `files`, `files/plain` |
| `C04` | `links` |
| `C05` | `files` |
| `C06` | `files`, `files/plain` |
| `D01` | `files` |
| `D03` | `links` |
| `D04` | `dirs` |
| `D05` | `files` |
| `E02` | `files`, `files/plain-moved` |
| `E03` | `dirs`, `dirs/B`, `dirs/B/plain-moved`, `files` |
| `E04` | `files`, `files/other` |
| `E05` | `dirs`, `dirs/B`, `dirs/B/moved-c`, `files` |
| `E06` | `dirs`, `dirs/B` |
| `E07` | `dirs`, `dirs/A-moved` |
| `E08` | `dirs`, `dirs/B`, `dirs/B/moved-A-again`, `dirs/B/moved-A-again/a`, `dirs/B/moved-A-again/sub` |
| `F01` | `dirs`, `files`, `files/other`, `files/plain`, `links`, `redirected-B` |
| `F02` | `dirs`, `files`, `files/other`, `files/plain`, `links`, `redirected-B` |
| `F03` | `dirs`, `files`, `files/other`, `files/plain`, `links`, `redirected-B` |
| `F04` | `files`, `files/plain` |

## 12. Observed xattr combinations

Across persistent upper forms, Stage 0 observed only these combinations:

| Inode | Xattr combination |
| --- | --- |
| directory | none |
| directory | `impure` |
| directory | `origin` |
| directory | `origin + impure` |
| directory | `opaque` |
| directory | `origin + redirect` |
| directory | `origin + impure + redirect` |
| regular | none |
| regular | `origin` |
| regular, intermediate `C06` | `origin + user.viewspace-stage0` |
| symlink | none |
| whiteout | none |

No `opaque+redirect`, `opaque+origin`, `opaque+impure`, redirect without
origin, symlink control xattr or whiteout xattr form was observed. Those
combinations are not silently generalized into the MVP grammar.

The only ordinary xattr actually exercised was:

```text
name  = user.viewspace-stage0
value = ordinary-xattr\n
```

It caused copy-up, was captured, and was later removed. It never affected the
Git oracle. MVP policy permits `user.*`, `system.posix_acl_access` and
`system.posix_acl_default` as recorded-but-Git-ignored metadata. POSIX ACL
xattrs were not exercised by Stage 0; their acceptance is explicit policy,
not an observed kernel form. Other xattr namespaces and every unknown
`trusted.overlay.*` name are rejected.

## 13. Pristine upper and workdir lifecycle

### 13.1 Pristine upper

At every `P00-before-mount`, upper contained exactly one entry: `.` was an
empty 1000:1003 directory, mode 0755, with no xattrs and no children. In
particular, no `trusted.overlay.*` marker existed before the first mount.

### 13.2 Workdir

Before first mount, work was exactly one empty outer directory:

| Field | Value |
| --- | --- |
| Path | `.` |
| Type | directory |
| Mode | 0755 |
| UID:GID | 1000:1003 |
| Children | none |

Across all 194 stopped states, the outer `.` remained a 1000:1003 directory,
mode 0755, with no xattrs. After a mount it contained the kernel-private
directory `work`, which was 0:0, mode 0000, with no xattrs. Eleven first
stopped captures additionally contained one 0:0, mode-0000, rdev-0 character
device inside that directory:

```text
work/#601  work/#623  work/#636  work/#643  work/#658  work/#65f
work/#666  work/#6c8  work/#6e1  work/#6fa  work/#715
```

Those entries occurred in `C04`, `D01`, `D03`, `D04`, `D05`, `E08`, and
`F01`–`F03`. They could be consumed on remount; `C04` exhibited exactly that
behavior while its upper and merged semantics remained correct. Although
these objects resemble upper whiteouts, their work-layer placement is
decisive: they are private work state, never a deletion instruction.

Therefore:

- work contents are captured for diagnosis but never classified as upper
  artifacts;
- work contents are never replay input;
- P03/P05 equality is required for upper semantics, not work internals;
- the paired workdir is retained only when remounting the same upper; and
- every new overlay generation starts with a new empty workdir.

The sync implementation must not traverse, clean, normalize or infer user
intent from workdir descendants.

## 14. Intermediate rename evidence

The two multi-step rename scenarios show how artifacts evolve.

### 14.1 `E05`: lower regular `A -> B -> C`

| State | Origin paths | Impure paths | Whiteouts |
| --- | --- | --- | --- |
| After `files/plain -> files/moved-b` | `files`, `files/moved-b` | `.`, `files` | `files/plain` |
| After `files/moved-b -> dirs/B/moved-c` | `dirs`, `dirs/B`, `dirs/B/moved-c`, `files` | `.`, `dirs`, `dirs/B`, `files` | `files/plain` |

No second final whiteout was left at `files/moved-b`. The original source
whiteout persisted while the copied-up file and parent metadata moved.

### 14.2 `E08`: lower directory, child edits and second rename

| Step | Redirect directory/value | Origin paths | Impure paths | Whiteouts |
| --- | --- | --- | --- | --- |
| 1. `dirs/A -> moved-A` | `moved-A` = `/dirs/A` | `dirs`, `moved-A` | `.`, `dirs` | `dirs/A` |
| 2. create `moved-A/new-child` | unchanged | `dirs`, `moved-A` | `.`, `dirs` | `dirs/A` |
| 3. modify lower child `moved-A/a` | unchanged | `dirs`, `moved-A`, `moved-A/a` | `.`, `dirs`, `moved-A` | `dirs/A` |
| 4. delete lower child `moved-A/sub/b` | unchanged | `dirs`, `moved-A`, `moved-A/a`, `moved-A/sub` | `.`, `dirs`, `moved-A` | `dirs/A`, `moved-A/sub/b` |
| 5. `moved-A -> dirs/B/moved-A-again` | `dirs/B/moved-A-again` = `/dirs/A` | `dirs`, `dirs/B`, redirected directory, its `a` and `sub` children | `.`, `dirs`, `dirs/B`, redirected directory | `dirs/A`, `dirs/B/moved-A-again/sub/b` |

This is the key replay example: the redirect supplies unchanged lower
children, the modified upper `a` overrides lower `a`, the pure-upper
`new-child` is added, and the child whiteout removes redirected lower `sub/b`.

## 15. Scenario-to-format provenance

### 15.1 Baseline and ordinary creation

| ID | Operation | Format result |
| --- | --- | --- |
| `A01` | Read/stat/readdir regular, executable, directory and symlinks | No persistent upper delta. |
| `A02` | Missing lookup and one deep read | No persistent upper delta or eager materialization. |
| `B01` | Create, overwrite and truncate binary regular | Ordinary non-executable regular; final bytes `74 65 78 74 00 58` (`text\0X`). |
| `B02` | Create regular, enable and disable execution | Both regular execute classes captured; final entry non-executable. |
| `B03` | Create relative and dangling symlinks | Ordinary symlink form with exact targets `files/plain` and `missing-target`. |
| `B04` | Create populated nested tree; create/remove empty directory | Ordinary directories/files plus `opaque=y` on `created-tree`; removed empty directory absent. |
| `B05` | UTF-8 and `.wh.*`-looking names | All three remained ordinary regular files; basenames had no control meaning. |

### 15.2 Copy-up and mutation

| ID | Operation | Format result |
| --- | --- | --- |
| `C01` | Overwrite lower regular | Copied-up regular with origin; copied-up parent/root impure metadata. |
| `C02` | Append then truncate lower regular | Intermediate and final copied-up bytes captured; no new control class. |
| `C03` | Add/remove execution on lower regular | Origin-bearing copied-up regular in both execute classes. |
| `C04` | Replace lower symlink with regular | Final ordinary regular covers lower symlink; parent `links` has origin and root has impure; no final whiteout. |
| `C05` | fsync-and-rename editor save | Ordinary replacement regular; parent `files` has origin and root has impure. |
| `C06` | Set/remove ordinary `user.*` xattr | Origin-bearing copy-up; user xattr recorded and Git-ignored, then removed. |

### 15.3 Deletion and type replacement

| ID | Operation | Format result |
| --- | --- | --- |
| `D01` | Delete lower regular | Exact whiteout at `files/plain`. |
| `D02` | Create/delete pure-upper regular | No final entry and no whiteout. |
| `D03` | Delete lower symlink | Exact whiteout at `links/relative`. |
| `D04` | Remove lower directory, recreate and add child | Intermediate directory whiteout; final opaque upper directory with ordinary child. |
| `D05` | lower regular -> directory -> symlink -> regular | Intermediate opaque directory; final ordinary regular with copied-up parent metadata. |

### 15.4 Rename and redirect

| ID | Operation | Format result |
| --- | --- | --- |
| `E01` | Rename pure-upper regular | Ordinary upper file moved; no OverlayFS control metadata. |
| `E02` | Rename lower regular in one parent | Source whiteout plus origin-bearing destination. |
| `E03` | Rename lower regular across parents | Source whiteout, origin-bearing destination and copied-up parent directories. |
| `E04` | Rename lower regular over lower regular | Source whiteout; destination replaced by origin-bearing source copy-up. |
| `E05` | Rename lower regular twice | One persistent original-source whiteout; origin/impure metadata moved with final destination. |
| `E06` | Rename pure-upper populated directory across parents | Final destination carried `opaque=y`; lower-backed parents carried origin/impure. |
| `E07` | Rename lower directory in one parent | Source whiteout plus relative redirect `A` and origin metadata. |
| `E08` | Cross-parent lower-directory rename, child create/modify/delete, second rename | Root-anchored redirect, source and child whiteouts, ordinary overrides/addition, origin and impure maps described above. |
| `E09` | Rename over nonempty directory fails | Expected error and no persistent state change. |

### 15.5 Lifecycle, access and isolation

| ID | Operation | Format result |
| --- | --- | --- |
| `F01` | Composite upper remount | Combined ordinary copy-ups, whiteouts, opacity and redirect survived remount with identical merged semantics. |
| `F02` | Same composite across git-viewspace-fs replacement | Exact composite remained usable against the replacement lower process. |
| `F03` | Three orderly lifecycle cycles | No new persistent form and no upper semantic drift. |
| `F04` | file/directory fsync plus syncfs | No artifact class beyond ordinary copy-up/origin/impure. |
| `G01` | Ordinary workload read/write/rename/delete | Confirmed ordinary-user merged behavior; no new format class. |
| `G02` | Lower immutability comparison | Primary and secondary lower trees remained unchanged. |
| `G03` | Three independent views, mutate only one | Mutated view contained one ordinary 0644 `isolated-change`; other uppers remained pristine. |
| `G04` | Bounded small/large structural-laziness measurement | Deliberately produced no raw artifact checkpoints and contributes no artifact form. |

### 15.6 Canonical composite upper

`F01`–`F03` deliberately combined the principal forms. At the final stopped
checkpoint the upper contained exactly the following logical inventory
(ordinary ownership was 1000:1003; whiteouts were 0:0):

| Path | Raw upper form | Bytes or control meaning |
| --- | --- | --- |
| `.` | directory 0755, `impure=y` | upper root |
| `composite-upper` | regular 0644 | `upper\n` |
| `dirs` | directory 0755, `origin+impure` | copied-up lower directory |
| `dirs/A` | directory 0755, `opaque=y` | recreated directory; lower children hidden |
| `dirs/A/recreated` | regular 0644 | `recreated\n` |
| `dirs/B` | exact 0:0 whiteout | remove lower `dirs/B` at its old name |
| `files` | directory 0755, `origin+impure` | copied-up lower directory |
| `files/exec` | exact 0:0 whiteout | delete lower executable |
| `files/other` | regular 0755, `origin` | unchanged copied-up bytes `other-v1\n` |
| `files/plain` | regular 0644, `origin` | `composite-copy-up\n` |
| `links` | directory 0755, `origin` | copied-up lower directory |
| `links/relative` | regular 0644 | `symlink-replaced\n`; replaces lower symlink |
| `redirected-B` | directory 0755, `origin+redirect` | redirect value `/dirs/B` |

The corresponding merged tree matched the independent oracle after an
ordinary clean remount (`F01`), after replacing git-viewspace-fs while exposing the
same immutable commit (`F02`), and through three orderly stop/start/remount
cycles (`F03`). This is evidence of remount compatibility and semantic
correlation, not a crash/reboot test or a proof of replay.

## 16. Sync interpretation required by this evidence

`view sync` must turn a stopped upper plus its immutable base commit into one
validated logical tree delta before changing the checkout.

At minimum, preflight must:

1. traverse the entire stopped upper without following symlinks;
2. validate every path, inode type, mode class, link count and xattr
   combination against this qualified grammar;
3. require ordinary upper directories, regular files and symlinks to carry
   the configured controller-wide workload UID/GID, with exact root ownership
   reserved for qualified kernel whiteouts;
4. reject every unknown `trusted.overlay.*` name, malformed recognized value,
   unsupported inode, special mode, hard link, invalid UTF-8 path/target or
   unqualified combination;
5. resolve each redirect against the immutable lower tree and reject missing,
   escaping, cyclic, conflicting or otherwise ambiguous targets;
6. choose the directory's original or redirected lower child set; replace
   that set with empty when the upper directory is opaque; remove names
   represented by upper whiteouts; then replace or add ordinary upper
   entries;
7. map ordinary regulars to Git 100644/100755, symlinks to 120000, and
   surviving directories to Git trees;
8. omit whiteouts, empty directories, origin, impure and accepted ordinary
   xattrs from the Git result.

Type replacement is structural. An upper directory over a lower
non-directory replaces that object and starts with no lower child set. An
upper regular file or symlink over a lower directory replaces the directory
and removes its whole lower subtree. Opacity controls inherited lower children
only where a corresponding lower directory child set exists.

The source whiteout and destination redirect are complementary parts of a
lower-directory rename. Treating the whiteout alone as deletion would lose the
tree; treating the redirect alone without suppressing the source would
duplicate it.

Origin tails must never be used to locate Git objects. Workdir entries must
never enter this algorithm.

Production artifact discovery and plan construction scale with the stopped
upper. They do not enumerate the lower tree to infer changes. Checkout
application may necessarily walk an affected lower subtree when opacity,
directory replacement, or deletion removes it; base qualification and Git
operations have their separately specified scaling behavior.

Replay tests must apply the plan through `view sync` and then compare the
resulting complete checkout tree with an independent oracle. That is a
post-sync validation seam, not an additional full-tree production preflight,
and Stage 0 did not implement or prove it.

## 17. Closed grammar and requalification triggers

The MVP parser is fail-closed. Unrecognized forms are errors, not requests to
guess kernel intent.

A new qualification decision is required if any of these change:

- kernel or OverlayFS behavior produces a new persistent inode/xattr form;
- upper/work cease to be the qualified ext4 layout;
- lower ceases to be the qualified git-viewspace-fs/FUSE profile;
- `redirect_dir`, `metacopy`, `index`, `nfs_export`, `xino`, `uuid` or
  `verity` changes;
- a relevant recorded OverlayFS module parameter or default changes,
  including `redirect_always_follow`, `redirect_max`, or `check_copy_up`;
- user-xattr OverlayFS encoding replaces trusted-xattr/device encoding;
- git-viewspace-fs ownership, mode or path-encoding policy changes; or
- the MVP begins supporting hard links, special files, additional xattr
  namespaces or other currently rejected states.

## 18. Explicit limits of the evidence

Stage 0 did not establish behavior for:

- actual reboot or sudden power loss;
- hard links;
- FIFOs, sockets, ordinary device files, capabilities, immutable flags or
  privileged user-created metadata;
- malformed raw uppers fabricated outside normal workload operations;
- unknown `trusted.overlay.*` values or combinations;
- POSIX ACL artifacts, despite their explicit ignored-metadata policy;
- special `renameat2` flags, swaps, cycles, redirect chains or path-limit
  boundaries;
- nested redirects or a multi-lowerdir stack;
- absolute symlink targets or a broad Unicode filename corpus;
- sparse-file and hole-punch allocation semantics; or
- other kernels, filesystems or OverlayFS option profiles.

These omissions are not implicit support. Replay tests may use in-memory negative
fixtures to prove fail-closed parsing, but accepting a newly observed positive
kernel form requires an explicit format/profile update.
