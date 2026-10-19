# Pinned submodules

Register top-level submodules with ordinary Git before importing a repository.
Use absolute repository URLs or absolute local paths. Relative URLs, nested
submodules, and edits inside module trees through views are not supported.

Repository initialization does not fetch modules. Acquire each exact pin explicitly:

```sh
git-viewspace --root ROOT submodule prepare --at main --path code/library
```

The daemon reads the commit's `.gitmodules` and gitlink, then fetches into a
URL-keyed bare store under `repo/.git/viewspace-submodules`. Reads never fetch.
Authentication uses ordinary Git credentials. Missing prepared objects produce
`SUBMODULE_NOT_PREPARED`. Acquisition has a 120-second process-group deadline;
the corresponding RPC waits up to 180 seconds.

To change a pin on a dedicated branch:

```sh
git-viewspace --root ROOT submodule prepare --at main --path code/library --commit MODULE_OID
git-viewspace --root ROOT branch-pin --branch module-update --commit SUPERPROJECT_OID
git-viewspace --root ROOT submodule pin --branch module-update --path code/library \
  --commit MODULE_OID --message "Update library"
```

Pinning refuses checked-out or writable-view-owned branches. Merge the resulting
commit deliberately. Existing views retain old pins; newly created views use
the pins of their chosen snapshot. URL changes select separate stores.

Module files are read-only in writable views. Synchronization rejects edits to
module paths, their registrations, or enclosing-directory replacements. Ordinary
file commits preserve gitlinks. Prepared objects have repository lifetime;
destroying a view does not delete them. Interrupted bare-store initialization
may require inspection and removal of that exact incomplete store.

This describes the current supported subset, superseding historical submodule
exclusions in the original implementation specifications.
