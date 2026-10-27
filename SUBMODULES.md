# Pinned submodules

Git Viewspace exposes prepared submodules as read-only directories, including
nested submodules. It reads Git objects directly: it does not create submodule
working copies or flatten their contents into the parent repository.

Register modules with ordinary Git before importing a repository. Registrations
come from each pinned commit's `.gitmodules`, not local submodule overrides.
Initialization, view startup and file reads never fetch objects.

## Explicit preparation

Prepare one top-level module and its complete pinned dependency tree:

```sh
git-viewspace --root ROOT submodule prepare --at main \
  --path code/library --recursive
```

Omit `--recursive` for a module without descendants. Nonrecursive preparation
rejects nested modules even if their objects are already available locally.
Preparation does not update a branch or alter existing views. Missing prepared
objects produce `SUBMODULE_NOT_PREPARED` before a view is mounted.

All modules use URL-keyed bare stores under `repo/.git/viewspace-submodules`.
The same URL and commit are acquired only once per traversal, even when mounted
at several paths. Different pins of the same URL share an object store. Exact
commits are retained by `refs/pins/OID`; successfully prepared objects survive a
later failure and are reused on retry. Stores have repository lifetime, not view
lifetime. An incomplete bare-store initialization may require inspection and
removal of that exact store before retrying.

Preparation has a shared 120-second deadline for fetches and local Git commands;
the RPC allows 180 seconds, including its normal queue waiting behavior. Fetch
timeouts terminate the fetch process group. Traversal is limited to 32 module
levels and 4096 module occurrences, counting repeated mount paths. Cycles and
limit violations are explicit errors. These are safety bounds, not flattening
or silent truncation policies.

While preparation runs, inspect `ROOT/run/submodule-prepare.json`. This bounded
file is atomically replaced with the current phase, module path, commit,
fetched/reused counts and timestamps. Terminal phases are `complete` and
`failed`; failures contain an error code. URLs and credential-bearing Git output
are excluded. A `running`, `checking`, `fetching` or `prepared` phase left by a
stopped/crashed daemon is stale diagnostics, not a resumable job. Submit
preparation again; available objects are rediscovered. No queue is restored from
this record.

## Repository locations

Supported locations are absolute local paths, `file://`, HTTP(S), SSH and Git
URLs, and SCP-style SSH locations such as `git@example.org:team/library.git`.
Git handles authentication through its ordinary credential mechanisms.
External Git remote helpers, URL query/fragment components and ambiguous plain
relative locations are unsupported. Relative traversal above the URL/path root
is rejected rather than reinterpreting the host or SSH path. Interior path
components retain their Git spelling; only leading `./` and `../` are resolved.

A module URL beginning `./` or `../` is relative to its containing repository's
URL. Git treats that repository as a directory: `../library.git` under
`https://example.org/team/application.git` resolves to
`https://example.org/team/library.git`.

For relative URLs at the root, first preparation freezes the root's `origin`
URL in `repo/.git/viewspace-submodules/source.json` (mode 0600). If there is no
origin, its absolute repository path is used. A relative local origin is first
made absolute against the repository location. Subsequent origin changes or
repository relocation do **not** reinterpret this binding. Preserve this file
with the object stores when moving the repository. To deliberately change an
acquisition location, commit an absolute module URL in `.gitmodules`; do not
edit the frozen base while views depend on it. Absolute-only registrations do
not require a frozen base. Descendant URL bases are their containing module's
resolved URL, including when identical commits exist at different locations.

## Updating pins

To change a top-level pin on a dedicated branch:

```sh
git-viewspace --root ROOT submodule prepare --at main --path code/library \
  --commit MODULE_OID --recursive
git-viewspace --root ROOT branch-pin --branch module-update --commit SUPERPROJECT_OID
git-viewspace --root ROOT submodule pin --branch module-update --path code/library \
  --commit MODULE_OID --message "Update library"
```

Pinning requires the full dependency tree to be prepared and refuses checked-out
or writable-view-owned branches. Merge the resulting commit deliberately.
Existing views retain their old pins; new views expose their chosen snapshot,
including all nested pins. Changing a nested pin requires an ordinary commit in
its containing repository, then admitting the updated outer commit. There is no
command that implicitly creates commits across several repositories.

Module trees remain read-only in writable and routed views. Top-level module
protection covers all descendants without extra nested mounts. Synchronization
rejects module edits, registration edits and enclosing-directory replacements.
Ordinary data commits preserve gitlinks. A routed subtree or external runtime
mount cannot bypass module protection.
