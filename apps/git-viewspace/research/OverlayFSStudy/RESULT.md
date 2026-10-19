# Stage 0 Execution Result

Date: 2026-08-11 UTC

Status: **PASS**

Canonical run UUID: `956a13f5-26e8-4ebb-8925-5783594d79aa`

Evidence bundle:
`/path/to/test-results`

Evidence manifest SHA-256:
`ecd14695ea35ad76cc90331def5b22e96b4c4fd25851d07fd90555588b23aa21`

The generated bundle is intentionally ignored by Git. This tracked result
pins its location, run identity, and manifest digest.

## Outcome

All 35 focused scenarios passed. There were no failed or omitted scenarios,
the artifact-format freeze completed, the specialized-scenario protocol and
sudo audit were valid, and cleanup was clear. All 3,936 files recorded in the
bundle checksum manifest independently verify.

The qualified host profile was:

- Debian Linux `6.12.96+deb13-cloud-amd64` on x86-64;
- ext4 upper/work storage on `/dev/sda1`;
- workload UID `1000`, GID `1003`, and umask `0022`;
- GitViewspaceFS `0.2.6+gitviewspacefs.3` on Python 3.13.5;
- read-only GitViewspaceFS with `allow_other`, `default_permissions`, and
  `negative_timeout=0`; and
- OverlayFS with redirects enabled and the fixed
  `metacopy=off,index=off,nfs_export=off,xino=off,uuid=null,verity=off`
  profile.

The run established that:

- configured Git commits are exposed with stable workload ownership and
  Git-compatible regular, executable, directory, and symlink modes;
- focused create, modify, truncate, replace, delete, rename, redirect, editor,
  remount, and orderly process-replacement behavior agrees with the independent
  Git-tree oracle;
- primary and secondary lower views remain unchanged across the mutating
  catalog;
- a redirect-bearing composite upper survives an equivalent GitViewspaceFS
  process replacement and three orderly stop/start/remount cycles;
- three simultaneous views are isolated, including two independent views of
  the same commit;
- a missing alias becomes visible immediately after valid configuration is
  published, without sleep or an intervening directory listing; and
- GitViewspaceFS is structurally lazy: the ordinary and 10,013-entry commits each
  required one Git tree-path lookup to pin the view and nine to read the same
  deep file, returning `deep\n` in both cases.

The lean large-tree check took 0.63 seconds. It used one fresh GitViewspaceFS
process and one mount per commit, read only the demanded deep file, and created
no checkpoint, remount, oracle-export, or full-tree capture cycle.

## Frozen artifact forms

The complete tracked account, including exact forms, occurrence maps,
transition behavior, field disposition, scenario provenance, and `view sync`
implications, is
[Qualified OverlayFS Upper Artifact Format](../../spec/OverlayFS-artifact-format.md).

The stopped upper evidence contains ordinary directories, regular files, and
symlinks plus the following qualified OverlayFS control forms:

- a root-owned character-device whiteout with `rdev=0`, mode `0000`, and no
  xattrs;
- `trusted.overlay.opaque=y` and `trusted.overlay.impure=y` at the exact
  recorded directory placements;
- strict UTF-8 relative and overlay-root-anchored
  `trusted.overlay.redirect` values; and
- the observed 33-byte `trusted.overlay.origin` layout with its fixed
  qualified prefix and variable opaque handle tail.

The retained machine-readable occurrence maps and field dispositions are in
the [qualification capsule](docs/README.md), specifically
[`docs/artifact-format.json`](docs/artifact-format.json). Stopped workdir
contents are captured losslessly but remain opaque kernel-private data: they
are not classified as upper artifacts and are not replay inputs. The `view
sync` implementation consumes the frozen positive grammar.

## Privilege and cleanup audit

All 732 audited sudo invocations succeeded and belonged to the allowed forms:
8 GitViewspaceFS/FUSE mount processes, 8 FUSE unmounts, 164 OverlayFS mounts, 164
OverlayFS unmounts, and 388 bounded read-only stopped-artifact captures.
Fixture construction, Git operations, workload mutations, oracle generation,
and evidence writes ran as the ordinary workload user.

A final read-only host inspection found no remaining GitViewspaceFS process, FUSE
mount, or OverlayFS mount.

## Deliberate exclusions

As specified, Stage 0 did not test actual reboot, hard links, unsupported
special files, privileged metadata injection, fabricated malformed raw
artifacts, raw block-device data, kernel journals, or process/shared-library
forensics.

## Validation

- Stage 0 harness: 45 tests passed with warnings treated as errors.
- GitViewspaceFS: 102 tests passed with warnings treated as errors.
- GitViewspaceFS wheel and source distribution built successfully; the installed
  wheel passed its dependency check.
- The complete canonical evidence checksum manifest verifies with no missing,
  extra, or mismatched file.
