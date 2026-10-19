# OverlaySync

`git_viewspace/overlay_sync.py <upper-dir> <copy-of-lower-dir>` replays a
**stopped** OverlayFS upper directory onto a disposable copy of the immutable
lower tree. It is the installed Python replay implementation for the qualified
artifact grammar in
[`../spec/OverlayFS-artifact-format.md`](../spec/OverlayFS-artifact-format.md),
and is intended to supply the replay core for `git-viewspace view sync`.

The adjacent [`git_viewspace/OVERLAY_SYNC_TODO.md`](../git_viewspace/OVERLAY_SYNC_TODO.md)
records measured follow-up performance ideas; it is not part of the replay
contract.

The prototype assumes the manager is the sole process changing the stopped
upper, target copy, and its staging paths while a sync is running. Concurrent
or adversarial path mutation is outside this study's scope.

The default mode validates the entire upper, captures any redirected source
subtrees before source whiteouts can remove them, and applies only the described
changes directly to the target checkout. Pass `--atomic-swap` to instead copy
and publish a completely synthesized sibling tree. Both modes copy only
Git-relevant state: path/type, regular bytes, executable class, and exact
symlink target. Overlay bookkeeping and other xattrs are not copied.
The direct mode leaves the checkout's root `.git` entry untouched.
The JSON result includes `git_pathspecs`, a minimal list of affected semantic
roots suitable for NUL-delimited `git add -A --pathspec-from-file`. An empty
list means there is nothing to stage. Consumers encode those strings as
NUL-delimited UTF-8 and invoke Git with `--literal-pathspecs`,
`--pathspec-from-file=-`, and `--pathspec-file-nul`.

The script starts as root because the qualified upper format uses root-owned
0:0 character-device whiteouts and `trusted.overlay.*` xattrs. It validates and
inventories that metadata first, then irreversibly drops supplementary groups,
GID, and UID to the ordinary workload identity recorded on the upper root.
Every checkout mutation and staging-path operation therefore runs as that
ordinary user and naturally creates ordinary-owned objects. It must never be
run against a mounted/live upper.

## Test suite

Each directory under `tests/cases/` contains exactly `A/` and `mutate.py`.
The harness copies the fixture into `~/.scratch`, mounts the scratch `A` as an
ordinary-directory OverlayFS lower at `Aprime`, applies the mutation to both
`Aprime` and independent oracle `B`, replays the stopped upper into `C`, and
compares `B` with `C` using Git tree semantics.

The 50 small cases cover:

- ordinary binary/executable files, symlinks, UTF-8 names, nested trees, and
  literal `.wh.*` filenames;
- lower copy-up, truncation, executable-class changes, atomic editor replace,
  deletion, and every regular/symlink/directory type replacement;
- pure-upper and lower-backed file/directory renames, including overwrite and
  repeated rename forms;
- populated-directory deletion, opacity, relative and root-anchored redirects,
  nested redirected-child deletion, redirected-child type replacement, and
  two independent redirects in one upper; and
- a compact composite containing ordinary creation, copy-up, whiteouts,
  opacity, and redirect together; and
- six light multi-operation batches covering mixed working-tree edits,
  redirected child edits, several simultaneous type replacements, opaque
  recreation, literal Git pathspec characters, and a mixed directory
  lifecycle.

Each successful case also remounts the same stopped upper/work pair and checks
the merged tree before replay. The harness checks exact expected whiteout,
opaque, and redirect paths, verifies one live-upper invocation is refused,
and requires no staging residue. The unprivileged regression tests cover
origin-envelope validation, Git execute classification, result-root type,
privilege-drop ordering, ordinary replay ownership, fail-before-change
behavior, pathspec construction, and redirect-plan rejection.

The additional repository round-trip suite uses a prebuilt
`repository.bundle` for every one of those 50 scenarios. It materializes three
independent repositories with external Git directories, creates a model commit
from a full independent `git add -A`, and creates the actual commit from the
sync report's NUL-delimited targeted pathspecs. It then deletes the actual
checkout and index, restores the checkout solely from the saved commit, and
only after that compares its commit tree and Git-relevant filesystem state with
the model. Both commits must have the fixture's base commit as their sole
parent, and both worktrees must be clean.

Each directory under `tests/repository-cases/` contains exactly
`repository.bundle` and a byte-identical copy of the corresponding
`mutate.py`. Regenerate the tracked fixtures after adding or changing a case by
writing to a new directory, reviewing the result, and replacing the old set:

```sh
python3 apps/git-viewspace/OverlaySync/tests/build-repository-fixtures.py \
  /path/to/new-repository-cases
```

Run from the repository root:

```sh
python3 apps/git-viewspace/OverlaySync/tests/run-tests.py
python3 apps/git-viewspace/OverlaySync/tests/run-commit-tests.py
python3 -m unittest discover \
  -s apps/git-viewspace/OverlaySync/tests -p 'test_*.py' -v
```

Both harnesses use sudo only for `/usr/bin/mount`, `/usr/bin/umount`, and the
`git_viewspace/overlay_sync.py` invocation. They retain each small run under
`~/.scratch` so root-owned OverlayFS artifacts are not removed through an
unapproved sudo cleanup operation.

## Primary references

- [Linux kernel OverlayFS documentation](https://docs.kernel.org/filesystems/overlayfs.html),
  especially “Directories,” “whiteouts and opaque directories,” “renaming
  directories,” and “Changes to underlying filesystems.”
- [Linux v6.12 OverlayFS source](https://github.com/torvalds/linux/tree/v6.12/fs/overlayfs),
  used to cross-check whiteout creation, redirect handling, and private xattr
  roles against the qualified kernel generation.

The tests intentionally use a normal ext4 directory as lower, per the test
contract, whereas the earlier Stage 0 qualification used git-viewspace-fs/FUSE.
Consequently the opaque bytes of `trusted.overlay.origin` may differ. The
parser validates its file-handle envelope but never uses it as identity or as
replay data. In particular, the plain-ext4 profile produced an origin-bearing
symlink when a lower symlink was renamed; the earlier FUSE qualification had
not observed that positive form.
