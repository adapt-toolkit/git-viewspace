# Independent specification review

A fresh read-only reviewer examined the specification, workplan, experiments and
current manager implementation. The review specifically challenged overengineering
and hidden ordinary-file costs. No implementation work preceded this gate.

Accepted recommendations:
1. Define external targets and reject managed-tree/recursive destinations.
2. Reject bindings inside read-only submodule trees rather than add another policy.
3. Bound restart identity guarantees: record dev/inode and faults, but do not attempt
   inode-reuse recovery after the anchor closes.
4. Reserve descriptor names and protect source/destination directory ancestors.
5. Enumerate the small special-file API and reject unsupported complex operations.
6. Require correct open-file registration ownership across rename, hardlinks,
   copy-up and unlink/recreate. Avoid a permanent registration cache. High-level
   libfuse remains preferable to implementing a separate low-level inode engine;
   actual namespace/handle tests will qualify the chosen bookkeeping.
7. Include private mounts/processes in offline status and partial-start cleanup.
8. Benchmark open/metadata with zero and many bindings.

These clarify the intended scope without adding replacement transactions, live
configuration, distributed locking, generation recovery or application-specific code.
Implementation may choose simpler mechanisms if they meet the explicit tests.

## Runtime-directory amendment review

A fresh independent read-only reviewer examined the proposed runtime bind mounts
against the existing specification and implementation. Recommendation: adopt as a
manager-only feature, not FUSE directory forwarding. Accepted the marker-file
solution to Git's inability to track empty directories, lifecycle ordering and
ownership checks, per-bind readonly verification, socket side-effect clarification,
and reuse of protected ancestor handling. The specification and workplan record
these requirements and their focused mounted tests. No broad socket-proxy system
or transactional directory semantics are introduced.
