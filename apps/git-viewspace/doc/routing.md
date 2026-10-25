# External files and runtime directories

An agent's working directory can contain versioned session data alongside files
that must remain external, such as credentials, and runtime objects such as Unix
sockets. Optional routing presents these together in one view. Ordinary file data
uses kernel FUSE passthrough; external regular files use synchronous routed I/O.
This is a filesystem integration feature, not a security boundary.

## Install the optional helper

The base Python installation is unchanged. Routing additionally requires a C++17
compiler, pkg-config, libfuse 3.17 or newer development headers/runtime, and a Linux
kernel with FUSE passthrough support (Linux 6.9 or newer). It requires privileged
mount and passthrough operations. The helper deliberately fails if passthrough is
unavailable; it does not silently substitute userspace copying for ordinary data.

From the source checkout:

```sh
python3 apps/git-viewspace-router/build.py --output "$HOME/.local/bin/git-viewspace-router"
```

The manager invokes the absolute helper path through non-interactive sudo. As with
other Viewspace helpers, do not grant untrusted users permission to replace an
executable that your sudo policy runs with privilege. No policy is installed for
you. The helper runs filesystem access under the view owner's filesystem identity.

## A routed regular file

Create and commit a descriptor in your source repository:

```text
.git-viewspace-#SF#-.credentials.json
```

Its contents are one absolute external filename, optionally followed by a newline:

```text
/private/agents/alice/credentials.json
```

The external regular file must already exist. The descriptor appears in the routed
view as `.credentials.json`; reads and writes access its external backing file.
The descriptor itself is hidden in that view. Git stores the descriptor and its
path, **not the backing contents**. Descriptor paths are consequently not secrets.

Enable routing while the view is stopped:

```sh
git-viewspace --root "$ROOT" view stop agent-a
git-viewspace --root "$ROOT" view routing enable agent-a \
  --executable "$HOME/.local/bin/git-viewspace-router"
git-viewspace --root "$ROOT" view start agent-a
```

The helper pins the external device/inode. In-place writes are immediately visible;
rename-over replacement, deletion and special-file hardlinks through the view are
rejected. Replacement or disappearance through the external path latches ESTALE
for that binding. Ordinary files remain usable. The fault survives view restart.
After deliberately checking the new backing file, stop the view and use:

```sh
git-viewspace --root "$ROOT" view routing rebind agent-a
git-viewspace --root "$ROOT" view start agent-a
```

Rebind accepts the current identities for all descriptors. A failed helper exit
can require rebind even if no individual fault record survived. A replacement manager leaves uncertain views stopped so that its control
interface remains available for rebind. Merely restarting does not silently
accept replacement credentials. Device/inode reuse after all
handles close is outside this identity guarantee.

Supported external-file operations include read/write, truncate, ordinary owner
permission changes, timestamps, flush and fsync. mmap, allocation, optimized-copy
operations and flock are unsupported. POSIX record locks are local to a FUSE mount;
they do **not** coordinate direct backing-file opens or other views. Applications
requiring that coordination are unsupported. Ordinary files retain their separate
kernel passthrough and FUSE locking behavior.

An application's atomic-save strategy must be tested: replacing a routed file is
intentionally rejected. Applications may also write temporary copies elsewhere in
the view. Those copies are ordinary files and would be committed by advance,
including ignored files. Quiesce applications and remove such temporary sensitive
material before publication. Routing is not a general secret-scanning mechanism.

## Runtime directories and sockets

For directory-shaped runtime state, use a manager-owned bind mount above the
router. Commit an otherwise empty destination containing this empty marker:

```text
session/runtime/.git-viewspace-runtime
```

Supply its external directory when enabling routing:

```sh
git-viewspace --root "$ROOT" view routing enable agent-a \
  --executable "$HOME/.local/bin/git-viewspace-router" \
  --runtime-dir session/runtime=/private/agents/alice/runtime
```

Multiple `--runtime-dir` options are allowed. Enabling again supplies the complete
configuration; omitting them clears the mapping. Both source directories and
tracked markers must exist. Paths may not overlap, traverse symlinks, overlap a
submodule/routed file, point back into the managed root, or contain subordinate
mounts. Sources backed by FUSE/OverlayFS are unsupported. This is a nonrecursive
bind mount, not a proxy implementation of Unix sockets.

Applications can bind and connect to Unix sockets through either directory path.
Runtime contents are never synchronized into Git; only the marker remains tracked.
The manager protects destination ancestors from rename and owns mount teardown.
Runtime directories use normal native filesystem semantics; they do not use the
regular-file pinned-inode policy above.

A read-only view also mounts its runtime directories read-only. File mutations
fail, but connecting to an existing socket can still affect its server. Read-only
filesystem access is not process or network isolation.

## Lifecycle and recovery

Stop/quiesce applications before advance, rebind, disabling routing or deleting a
view. The manager stops runtime binds, then the router, then its private underlying
view. Busy or foreign mounts are reported rather than silently discarded.

A partial startup may leave owned runtime records. The normal stop/recovery path
uses recorded mount identities. An unrecorded or replaced mount requires operator
inspection; recovery does not guess ownership. Per-view routing state and helper
stderr reside under the view's private `routing/` directory, outside Git. Do not
edit these records while the daemon is active.

Writable view restart also clears stale OverlayFS origin handles while the upper
layer is unmounted. See the [restart recovery rationale](../../../development/routing/overlay-origin-recovery.md).

For measured ordinary data/metadata costs and remaining qualification boundaries,
see [routing performance](../../../development/routing/performance.md). Passthrough
avoids userspace copying of ordinary data but does not eliminate FUSE metadata
roundtrips; workloads dominated by opening many tiny files have higher overhead.
