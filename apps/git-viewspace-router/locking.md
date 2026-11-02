# Passthrough and flock ownership

The file description registered with `FUSE_DEV_IOC_BACKING_OPEN` must differ from
the handle description used by `flock`. Kernel passthrough references can survive
the router when another mount namespace retains the FUSE superblock. Registering
the locked description can therefore retain locks after both client and router
exit, preventing a new writer from opening the same backing inode.

At the first registration for a path, the router reopens the already admitted
handle through `/proc/self/fd`, registers that independent description, and closes
the temporary descriptor. `dup` is insufficient because it shares lock ownership.
The original handle retains normal locking semantics, including rename, unlink
and subsequent permission changes. Reopening retains access/status flags without
reapplying creation, truncation, exclusive-create or no-follow behavior. No extra
persistent descriptor is retained per handle.

Run `tests/git-viewspace-router/test_flock_lifetime.py ROUTER_BINARY` with mount
and namespace privileges and a passthrough-capable kernel. `FIXTURE_UID` selects
the backing/client identity. It verifies shared/exclusive locks, nonblocking
conflicts, close, rename/unlink, permission changes, data retention, and router
death while a private namespace retains the mount. An unrelated control mount
must remain usable. All mounts and processes are disposable.

This change applies to newly launched routers. Existing routers must not be
restarted indiscriminately. Existing orphan locks require separately verified
writer absence and explicit recovery; the router never bypasses writer checks or
automatically replaces lock files.
