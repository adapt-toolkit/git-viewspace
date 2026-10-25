# Overlay origin recovery prerequisite

## Finding

The current lower filesystem uses high-level libfuse2, whose internal file handles
are not stable across daemon lifetimes. OverlayFS records lower handles in
`trusted.overlay.origin`, even with indexing and NFS export disabled. A reused
handle may resolve differently after restart. Mounted experiments reproduce
lookup-order-dependent ESTALE on copied-up files without the routing layer.

Neither `st_ino` stability alone nor disabling the older EXPORT_SUPPORT flag is a
proven correction: cached inode decoding can still resolve a reused node ID.
The kernel's newer NO_EXPORT_SUPPORT capability is a cleaner future option, but
requires a different lower-FUSE binding than the present fusepy/libfuse2 stack.

## Narrow solution under test

When the lower FUSE incarnation or lower revision changes, before mounting an existing writable generation, while it is exclusively owned
and unmounted, remove only `trusted.overlay.origin` from its upper tree. This
configuration already fixes index, metacopy, NFS export and xino to off. Full
copied-up contents therefore reside in the upper tree; directory redirects remain
path-based and are preserved in their separate xattrs. Never apply this cleanup
to arbitrary external OverlayFS trees or configurations.

Preserve content, ownership, modes, upper inode/link topology and every other
xattr, especially redirect, opaque, whiteout and application attributes. Removing
an xattr changes ctime; stable ctime and overlay-visible inode numbers across a
remount are not promised. Do not force a commit or reset uncommitted changes.

The helper must enumerate every entry without following symlinks, including
symlinks reported as directories. Traversal/removal errors abort mounting. It runs
under the manager's mutation exclusion and must reject an upper already mounted
or containing subordinate mounts. Recovery is idempotent after interruption:
remaining origins are removed on the next attempt before mounting.

## Review and experiments

An independent read-only reviewer inspected Linux 6.12 lookup code and this
proposal. They found no content-loss mechanism under the fixed configuration,
requested copied-up hardlink and untouched redirected-child tests, and highlighted
ctime changes and symlink traversal. Those concerns are adopted.

The expanded mounted matrix exercises routing on/off, changed lookup order, fresh
copy-ups between restarts, regular-file edits, deleted lower files, opaque directory
replacement, nested directory redirects with untouched lower descendants, copied-up
hardlinks modified through both names, symlinks, permissions, and user xattrs.
A final publication check must verify the committed tree against the view.

References:
- https://docs.kernel.org/filesystems/overlayfs.html#changes-to-underlying-filesystems
- https://github.com/torvalds/linux/blob/v6.12/fs/overlayfs/namei.c
- https://github.com/torvalds/linux/blob/v6.12/fs/fuse/inode.c
- https://github.com/libfuse/libfuse/blob/fuse_2_9_bugfix/lib/fuse.c

## Implementation qualification

The manager now invokes the isolated origin-reset helper immediately before each
writable mount. The helper rejects active upper/submount use, never follows
symlinks, and fails closed on errors. Eight automatic manager restart cycles
passed with alternating routed/unrouted views and no manual origin cleanup.
The larger exploratory matrix passed 24 restart cycles. A separate supported-tree
publication fixture matched all 28 files, executable modes and symlink targets
exactly. Existing hardlink and nested-redirect publication restrictions remain;
the reset does not broaden OverlaySync's supported artifact grammar.

Independent implementation review found no correctness blocker within the stated
exclusive stopped-upper contract. Its host-test PATH portability finding was
fixed: tests locate the CLI through PATH rather than assuming Python's directory.
Unit coverage includes active mount rejection, symlink handling, mutation limited
to the origin attribute, and failure propagation. Mounted coverage is registered
as the exhaustive privileged `origin-restart-host` case.

## Same-incarnation remount optimization

`origin-epoch.json` in each upper generation records the unique lower-FUSE source
UUID, lower commit/alias and upper generation after a successful cleanup. Identical
epochs skip cleanup. A new lower process has a new source token and therefore still
requires recovery; manager PID reuse cannot bypass it. An interrupted cleanup does
not publish the new epoch. Four alternating routed/non-routed daemon restarts with
three same-daemon remounts each passed the content/redirect/hardlink regression.
This avoids routine scans but does not eliminate recovery-time upper traversal.
