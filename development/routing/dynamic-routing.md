# Dynamic routing workstream

This amendment supersedes the activation-time descriptor inventory and hidden,
immutable descriptor namespace in specification.md. Work is confined to the
routing branch; agent-operator integration and notification delivery are excluded.

## Required behavior

- The descriptor is the sole routing declaration. No explicit file list and no
  recursive view scan at activation or normal lookup.
- A directory shows both `.git-viewspace-#SF#-NAME` and virtual `NAME`. A deleted
  descriptor remains visible but its virtual name does not.
- Descriptors are readable. Direct creation, writes, truncation and writable mmap
  are rejected. A prepared ordinary file is installed by atomic rename; validate
  it before changing the old descriptor. Invalid replacement leaves the old one.
- Deleting the descriptor removes routing, without touching external contents.
  Deleting its virtual target atomically adds a deleted marker to the descriptor,
  also leaving external contents untouched. Reactivation replaces the descriptor.
- Descriptor replacement affects subsequent opens. Already-open target handles
  retain their backing object. Unexpected external pathname replacement remains
  distinct from descriptor replacement and must not silently redirect a handle.
- Read-only views reject these mutations. Real-file/virtual-name collisions and
  malformed declarations produce explicit errors, not ordinary-file fallback.

## Implementation and review plan

1. Resolve only the adjacent descriptor on demand, using the underlying view.
   Directory enumeration processes only the directory being enumerated. Preserve
   ordinary kernel data passthrough and bound retained binding handles/cache.
2. Atomic descriptor installation must not leave a writable alias: reject source
   hardlinks and open source handles, and serialize relevant namespace operations.
   Descriptor editing outside the router while mounted is unsupported. Validate
   external targets, protected runtime directories and submodule boundaries lazily.
3. Persist admitted external identities only for encountered bindings, preserving
   restart faults without a second authoritative routing inventory. Descriptor
   replacements must be distinguishable from unchanged descriptors after restart.
4. Remove unconditional origin cleanup from ordinary remounts only with proven
   lower-filesystem identity guarantees. Investigate eliminating stale lower
   handles first; do not silently remove recovery protection for performance.
5. Test create/replace/delete/reactivate, invalid replacement, open handles,
   aliases, collisions, directory pagination, readonly behavior, restart and
   commit exclusion. Use disposable mount roots and preserve full diagnostics.
6. Measure startup against increasing unrelated tree sizes, ordinary metadata
   operations, bulk I/O and repeated mount/checkpoint cycles. No metadata-cache
   change may hide externally observable routing changes or permission failures.
7. Obtain independent implementation review, run applicable regressions, update
   public documentation, commit and push feature/routed-external-files. Partial
   subtree views are a fallback only if the direct approach remains inadequate.

## Review notes

The initial independent review identified that renaming an open writable temp
file would permit later descriptor mutation through its old handle. Installation
therefore needs inode-level open-handle and hardlink checks, including mappings.
The review also cautions that an origin-reset epoch must identify the actual lower
FUSE incarnation and lower revision, not merely a manager PID. Same-daemon handle
churn needs a regression before relying on an epoch optimization.
