# Optional routed external files

Status: implementation specification. Dynamic descriptor semantics and optional
subtree exposure supersede the initial static design. See
[dynamic-routing.md](dynamic-routing.md) and the authoritative current
[operation contract](../../apps/git-viewspace/doc/routing.md).

## Purpose

Applications sometimes keep durable working state alongside credentials or other
files that must remain outside Git. A routed view exposes native filesystem paths
without committing external contents or making applications understand symlinks.
A read-only view remains entirely read-only, including external bindings.

The feature is opt-in. Existing installations, views, package entry points and
non-routed performance must remain unchanged. It is an ordinary filesystem feature,
not an agent-specific mechanism or a security sandbox for hostile same-user code.

## Representation and namespace

A tracked regular descriptor named `.git-viewspace-#SF#-NAME` exposes `NAME` in
its containing directory. NAME must be a nonempty normal basename, excluding `.`
and `..`. Leading dots are preserved. Descriptor contents are one absolute UTF-8
path to an existing regular external file, with an optional final newline.
Descriptors contain bindings, never external contents. Final symlink targets,
devices, FIFOs and directories are rejected. No glob, directory routing, command
expansion, credentials-specific parsing, or dynamic configuration language.

Descriptors and virtual names are both visible. Resolve adjacent descriptors on
lookup, without a startup inventory. Atomic rename installs/replaces a validated
descriptor; direct edits are prohibited. Unlinking a virtual target records deletion
in its descriptor and leaves external data untouched. Descriptor removal is allowed.
Old target handles keep their backing object. See the operation contract for exact
format, collision, restart, alias and reactivation rules.

## Routing architecture and performance

Optional public FUSE router at views/NAME/mnt. A private underlying mount presents
the existing read-only Git view or writable OverlayFS view. Existing submodule
read-only mounts remain beneath the router and retain their protection.

A separate, small C++17 executable uses libfuse >=3.17 and Linux kernel passthrough.
Ordinary data I/O uses kernel passthrough; userspace forwards metadata/namespace
operations. A registration is shared by concurrent handles of the same exposed
file, and released after the last handle. Rename/unlink of open ordinary files
must preserve normal handle behavior; test rather than assume library handling.
No descriptor rescans, content hashing or special-file stat calls on ordinary
read/write operations. Namespace lookup uses lazy adjacent-descriptor reads and a bounded positive cache.
No negative pathname inventory or full-tree scan is maintained. Runtime mounts alone
have a precomputed ancestor-protection set.

If passthrough cannot be negotiated or a backing registration fails, return a
clear error; do not quietly introduce a slow fallback. Non-routed views still work.
A conventional userspace forwarding probe exists only for measurement comparison.

The executable requires mount/backing-registration privileges. It must execute
backing filesystem operations with the managed owner's filesystem UID/GID and
groups, not root's discretionary access privileges. Normal FUSE permission checks
remain enabled. Private mount directories remain owner-accessible only. Routed
external files must not become a way to read files the view owner cannot read.

## Special-file semantics

At explicit binding admission, pin the external file's device/inode identity.
Retain an open anchor descriptor while active. Every routed operation validates
that the external pathname still names that identity. Actual operations use retained
handles to the pinned inode, never a potentially replaced pathname.

Missing/replaced/nonregular backing paths fault the binding with ESTALE and a
structured diagnostic. The fault remains until explicit rebind; unrelated files
and bindings continue functioning. Persist enough operational identity/fault state
outside Git to avoid silently adopting a replacement after daemon restart. Ordinary
in-place modifications are visible; no content snapshot or transaction isolation
is promised. A check/use race may affect the original inode before replacement is
detected; the router cannot serialize unrelated external writers.

Allow read, write, truncate, flush/fsync and ordinary file metadata operations that
preserve identity and satisfy backing permissions. Virtual unlink records deletion without touching backing contents. Reject rename-away,
rename-over, hardlink creation involving a binding and replacement with a symlink.
Do not implement temporary-file-to-special rename by copying contents: applications
requiring atomic replacement are deliberately incompatible with this first version.
All writes preserve the backing inode. Read-only views reject all mutating operations
with EROFS, even if the external file is writable by the owner.

Special handles must disable data caching/passthrough so each read/write reaches
the identity check. Memory-mapped special-file access is outside the supported
contract; do not advertise coherent mappings or add a VM fault-handling subsystem.
Known unsupported operations fail where the FUSE interface permits. External
contents written by an application into some *other* ordinary path are not magically
classified or filtered; only declared bindings are excluded from Git.

## Lifecycle, configuration and publication

Extend per-view JSON configuration with optional router settings; absence preserves
the existing format's meaning. CLI supports enabling/disabling routing while stopped,
selecting the installed executable, and explicit rebinding while stopped. Commands
must update configuration through the manager, not require hand-edited state files.
Router table/identity/fault state and logs live under an owned per-view operational
directory outside the repository. No external content bytes in these records.

Start: validate configuration (descriptors are validated lazily), set up underlying existing mounts,
start router, verify its public mount identity, then report started. Failure unwinds
only mounts/processes created by this attempt. Stop: unmount public router, reap its
process, then stop underlying mounts. Refuse foreign mounts or uncertain ownership.
Normal stop must not use lazy unmount to mask a busy view. Existing explicit force
cleanup semantics remain available.

Advance operates on the original underlying overlay upper tree. The router never
creates a regular overlay file at a special visible path, so only the descriptor
can enter the Git commit. Stop/advance/restart must retain routing configuration.
Commit switches and Git rollback restore descriptors, not external data. Changed
descriptors select their declared target lazily; unchanged declarations keep admitted identity.
Read-only and writable views both use this lifecycle; disabled views are unchanged.

## Explicit exclusions

No direct in-place descriptor writes, implicit external replacement recovery,
asynchronous mirroring, last-writer arbitration, automatic secret detection,
transactional external writes, kernel module, external-writer locking protocol,
or portability beyond the documented Linux/libfuse prerequisites. No production
harness migration or changes to unrelated repositories/services in this workstream.

## Acceptance evidence

Actual mounted tests (not mocks alone): descriptor visibility/atomic editing/collision/path validation;
read/write/truncate/fsync preservation of inode; externally modified bytes visible;
external replacement and disappearance produce latched faults; explicit rebind;
all read-only mutation attempts fail; ordinary multi-handle copy-up, rename/unlink,
hardlinks, symlinks, permissions, directory enumeration, submodule read-only behavior;
advance and rollback keep descriptors and never commit backing content; repeated
start/stop, start failure cleanup, restart fault preservation and absence of leaked
mounts/processes. Root-only backing file access denied to ordinary view owner.

Measure warmed random read/write median/p95/p99 and throughput at 4 KiB, 64 KiB,
1 MiB and 4 MiB over multiple files and concurrent workers; compare direct and routed
OverlayFS under matched conditions, and routed/non-routed read-only Git views.
Measure metadata and open/close separately. Verify ordinary passthrough by callback
counters, not throughput alone. Report the complete scope/limits of measurements.
Run unchanged baseline unit and mount-enabled acceptance suites, then independently
review implementation and test coverage for complexity and hidden performance costs.
Update public README, CLI help, architecture, prerequisites, security and examples;
submit a PR with evidence and any deliberate limitations.

## Review resolutions and boundary details

- External destinations must resolve outside the entire managed root (repository,
  overlay storage and all public/private mounts). Reject destinations backed by a
  FUSE or OverlayFS filesystem to avoid recursive routing. This is not a defense
  against a malicious same-user operator creating hidden aliases/hardlinks.
- Descriptors in read-only submodule trees are rejected, rather than introducing
  another per-binding access-policy mechanism. Ordinary submodules remain usable.
- Descriptor-prefixed names are readable but cannot be directly created or modified.
  Atomic rename and unlink follow the current operation contract. Runtime directory
  ancestors retain rename protection; ordinary directory moves preserve admissions.
- Persist admitted device/inode and latched faults on transitions only. Comparing
  saved identity after a normal restart cannot detect inode-number reuse after the
  old anchor closes; that rare case is explicitly outside the guarantee. No inode
  generation/filehandle recovery system is added.
- Special-file supported operations are open/read/write, getattr, truncate,
  chmod/chown subject to normal owner permissions, timestamps, flush and fsync.
  Reject special-file hardlinks, rename, allocation, ioctl, flock and cross-file optimized-copy operations. POSIX record locks
  are local to the FUSE mount: they do not coordinate separate views or direct
  opens of the external file. Applications requiring such coordination are
  unsupported. Ordinary files retain the kernel FUSE record-lock behavior needed
  by applications such as SQLite. No special-file mmap coherence
  guarantee. Ordinary metadata and handle behavior remain separate.
- Open-file registration ownership is distinct from names: rename/unlink/recreate
  must not attach a new file to an old registration. The high-level library's open
  file hiding may assist, but implementation must prove mixed-access opens, copy-up,
  hardlink aliases and rename/unlink/recreate tests. Do not introduce a persistent
  inode cache. Direct modification of the private ordinary backing tree while
  mounted is unsupported; all ordinary mutations pass through the router.
- Offline status, root validation, partial-start cleanup and recovery must include
  private mounts/router processes even when the public mount is absent. Reuse
  existing ownership checks; do not add a second daemon-management framework.
- Benchmark metadata/open costs with zero and many bindings to detect table scans
  or unrelated locking overhead. Successful ordinary access never persists state.

## Amendment: external runtime directories

Optional runtime directory bindings complement pinned regular files. They are
manager-owned Linux bind mounts above the public router, not FUSE forwarding of
socket inodes. Each maps a normalized relative destination to an existing absolute
external directory. Configure only while stopped. The destination must contain
exactly one tracked empty `.git-viewspace-runtime` marker and no other entries;
Git cannot retain an empty directory. Validate the actual underlying tree at each
start. The runtime mount hides the marker, and commits retain only that marker.

Reject root destinations, dot/dot-dot or symlink components, overlapping targets,
submodule destinations, intersections with regular-file bindings, sources inside
the managed root, FUSE/OverlayFS sources, and sources containing subordinate mounts.
Use nonrecursive binds. Do not create external directories or copy their contents.

Start the base and router first, install and verify runtime mounts, then report
ready. Applications must wait for start success. Stop runtime mounts before the
router and base. Record mount identities for recovery; never unmount a foreign
replacement. Partial-start cleanup removes only owned mounts. Busy normal unmount
remains an error. Status includes runtime mounts even if the public router vanished.
Protect destination ancestors from rename/replacement using the router's existing
protected-directory mechanism.

Each runtime bind is nosuid,nodev and inherits the view's filesystem-readonly
setting. A readonly parent alone does not make a child bind readonly; remount and
verify its flags. Readonly prevents filesystem changes through the view, not Unix
socket communication or external effects of that communication. Views are not
security isolation boundaries.

Runtime directories use native filesystem semantics: changes are immediately
visible, while a mounted source directory remains its original inode if its
external pathname is replaced. No pinned-file fault/rebind logic applies to these
directories. Deliberate application copies outside the runtime mount are ordinary
files and may be committed. This amendment supersedes the original directory-map
exclusion only for these explicit manager-owned runtime mounts.
