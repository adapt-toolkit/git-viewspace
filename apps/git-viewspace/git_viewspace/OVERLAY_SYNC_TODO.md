# File-operation performance TODO

git-viewspace met the quick benchmark with a persistent git-viewspace-fs process,
one reused checkout, direct OverlaySync replay, targeted Git staging, and a
bounded immutable-tree lookup cache. The next performance work should focus on
the first modification of lower-backed files:

```text
application -> OverlayFS copy-up -> git-viewspace-fs/FUSE reads
            -> Git object lookup -> ext4 upper
```

Once a file has copied up, later operations mostly use the ordinary ext4 upper
and should be substantially cheaper.

## Recommended next experiment

Completed 2026-10-05: a shared byte-bounded content LRU removes repeated decoding for
resident blobs across reads/reopens/views; regular-file metadata uses ODB headers.
The default budget is 256 MiB, configurable at startup. No disk cache or new FUSE handles.

1. Measure remaining oversized-file workloads before considering FUSE handles or a
   different oversized-read strategy. Files exceeding the budget currently bypass retention.
2. Enable safe positive kernel caching within pinned immutable aliases.
   Investigate longer attribute and entry timeouts, kernel content caching,
   read-ahead, and larger FUSE reads. Keep `negative_timeout=0` so newly
   configured view aliases remain immediately visible.
3. Record callback counts and read sizes while rerunning the same 50-file
   benchmark. Measure lower-backed copy-up separately from pure-upper file
   creation.

## Additional hypotheses

4. Add one internal `stat_entry(commit, path)` operation that returns type,
   size, mode, and timestamps together instead of making separate lookups.
5. Byte-bounded blob-content LRU is implemented; do not introduce an unbounded cache.
6. Keep active OverlayFS views mounted, just as git-viewspace-fs remains persistent,
   so normal ongoing edits avoid mount lifecycle overhead.
7. Consider multithreaded git-viewspace-fs for genuinely parallel workloads after
   checking pygit2/libgit2 threading behavior. It is not expected to help the
   current sequential benchmark much.
8. Longer term, consider a low-level inode-oriented FUSE implementation that
   associates kernel inodes with resolved `(commit, tree entry)` objects and
   avoids repeated pathname traversal.
9. Reconsider `metacopy=on` only if measurements show metadata-only operations
   are important. It would expand the qualified OverlayFS artifact grammar and
   does not avoid content copy-up for ordinary writes.

Prefer measured, bounded changes. Preserve immutable-commit semantics, keep
the common path proportional to the overlay delta, and avoid expanding MVP
scope solely for speculative optimization.
