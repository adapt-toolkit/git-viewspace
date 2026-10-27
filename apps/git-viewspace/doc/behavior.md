# Behavior and limitations

## View semantics

A read-only view exposes exactly one immutable Git commit. A writable view adds
one persistent OverlayFS upper/work generation over that commit. Ordinary file
operations occur through the public mount; the lower commit remains immutable.

Git-relevant state consists of:

- relative path and entry type;
- regular-file bytes;
- Git executable class from the owner executable bit; and
- exact UTF-8 symbolic-link target bytes.

Git does not preserve directory entries, ownership, timestamps, inode
identity, allocation, ordinary non-executable permission distinctions, or
ordinary xattrs. Empty directories therefore disappear from committed trees.

The supported repository model is deliberately narrow: ordinary complete
SHA-1 repositories with regular files, executable files, symlinks, and trees.
Submodules, sparse and shallow repositories, linked worktrees, alternate
object stores, filters, working-tree encodings, hard-linked upper entries,
special files, invalid UTF-8, and unknown OverlayFS metadata are rejected.
System/global Git configuration and custom repository merge drivers are not
used by managed Git operations.

## Sync and the singular checkout

One root owns one ordinary checkout at `<root>/repo`. The workload is expected
not to use it directly. `view sync` resets that checkout to the writable view's
recorded base and replays the stopped OverlayFS upper into it.

The privileged helper inventories and validates the complete upper before
direct replay mutation, then irreversibly becomes the root's recorded ordinary
UID/GID before writing checkout paths. Whiteouts become deletions, opacity
removes inherited children, redirects become directory moves, and ordinary
upper entries replace or merge with lower entries according to the qualified
OverlayFS grammar.

The resulting manifest is derived from the validated upper artifacts. It is
not a user-selected list of files.

## `.gitignore` and unconditional staging

The current workflow intentionally commits every Git-relevant change made
through the writable view. `.gitignore` does not remove an overlay artifact
from the sync manifest.

`view stage` uses the equivalent of:

```text
git --literal-pathspecs add -A --force \
  --pathspec-from-file=- --pathspec-file-nul
```

with NUL-delimited input. Large manifests may use fewer directory roots for
execution. In that case staging occurs in a temporary index and its complete
delta is checked against the **original** sync manifest before installing the
index. Broader execution roots do not authorize additional changes. Small or
modest sparse manifests retain literal staging to avoid unnecessary broad scans.
Consequently:

- a newly created ignored file is staged and committed;
- modified or deleted tracked files are staged even if a new ignore rule
  matches them;
- a changed `.gitignore` is staged like any other file;
- if that `.gitignore` ignores another file created in the same view, both
  files are staged; and
- repository-local and global exclude rules do not suppress a manifest path.

After targeted staging, the manager verifies that no changed or untracked path
remains. Commit hooks and commit signing are disabled, so they cannot change or
reject the managed transition.

Compact staging holds Git's normal `index.lock`; Git uses `index.lock.lock`
internally for the temporary index. Ordinary failures release owned locks and
preserve the prior index. A killed process may leave these locks, requiring the
same stopped-writer verification and lock recovery as an interrupted Git command.

This differs from an ordinary interactive Git worktree. It is a deliberate
MVP invariant: after commit there is no unstaged or ignored filesystem residue
to carry from the old OverlayFS base to the new commit.

The current implementation does **not** support partial staging, user-selected
paths, or preserving uncommitted/ignored files across `view advance`. A future
native-Git policy would need to capture the residual working-tree delta,
switch to a fresh overlay generation, and reapply that delta through the new
public mount. Simply retaining the old raw upper is unsafe because its
whiteouts, redirects, and origin metadata are tied to the old lower commit.

Partial staging and residual replay remain outside the supported interface.

## Commit behavior

`view commit` commits the exact staged index with the view's recorded base as
its sole parent and the root's configured manager identity. If the staged tree
equals the base tree, it records a no-op transition and creates no empty
commit.

Commit alone does not change the view's lower. The subsequent `clean` discards
the old upper while preserving the pending result; `switch --next` publishes a
new immutable alias and fresh empty generation at the resulting branch tip.
`view advance` performs the complete sequence and starts the resulting view.

## Parent merge behavior

`branch merge` is separate from view publication. It computes a two-parent
merge directly from an exact attempt tip and the current parent tip, then
compare-and-updates only the parent ref. It never changes an overlay or deletes
the retained attempt branch. The parent must be a direct local ref that is not
claimed by a writable view. The default policy refuses a checked-out parent;
the explicit `detach-if-clean` policy accepts one only at the exact parent tip
with a clean index and worktree, then detaches HEAD without rewriting checkout
contents. Conflicts are found before detachment, leave both refs unchanged,
and return a bounded `conflict_paths_rr` list. A failure after detachment can
leave the checkout cleanly detached at its old tip. An already-incorporated
attempt is an idempotent success and performs no detachment.

## Immutable branch-pin behavior

`branch-pin` is a repository-only compare-and-create operation. It accepts one validated direct
local branch name and one exact lowercase 40-character SHA-1 ID whose object is a commit. It does
not peel symbolic names or tags, accept abbreviations or uppercase spellings, or require the commit
to be reachable from another ref. Ref creation compares against the all-zero absent value and does
not dereference symbolic refs.

An absent ref is created with `created: true`. An existing direct ref at the same commit, including
one created concurrently, is idempotent success with `created: false`. A different direct tip is
`BRANCH_DIVERGED`; a symbolic ref is `SYMBOLIC_BRANCH_UNSUPPORTED`. Invalid branch and commit
spelling, a non-commit object, and a missing object fail before ref creation. The operation never
moves or deletes an existing ref.

Pinning has no view effect: it creates no view or alias, acquires no branch claim, changes no mount,
index, worktree, or checkout attachment, and emits no independent view transition. Publication
source refs are retained so later inbox delivery can still merge the exact commit after the sender
advances. There is no automatic cleanup or deletion operation, and the manager does not police
manual ref edits outside an admitted pin request.

## Access and privilege assumptions

The same non-root UID/GID initializes and operates a root. The MVP assumes that
this user will access repository content through public views and will not
modify manager state or the singular checkout directly. It does not attempt to
isolate those paths from that same user.

Privilege is restricted to mount/unmount, passive upper inspection, and the
OverlaySync entry boundary. Replay itself drops to the recorded ordinary
identity before modifying checkout files.

## Concurrency

Each root has one daemon and one serialized mutation stream. The design does
not protect against a second uncontrolled process modifying manager storage or
the checkout during an admitted command. Multiple repositories are managed by
multiple independent roots and daemons.
