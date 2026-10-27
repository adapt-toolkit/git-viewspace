# Efficient staging plan

Experiments on a disposable ext4 volume, Git 2.43, show literal pathspec cost
growing with manifest size and index size. On 500,000 entries / 140,000 changes,
the original command exceeded 90 seconds; 140 directory roots took 4.6–4.9s,
exact update-index took 3.6s. A single common root took 36.5s. All successful
methods produced the same tree. These are warm-cache synthetic tiny-file tests,
not production latency forecasts. No host cache flushing or production changes.

Keep native git-add semantics rather than implementing file/mode/symlink and
directory replacement staging ourselves. Keep the existing path for manifests
of at most 256 paths. For larger manifests, use immediate parent directories,
remove redundant descendants, and fall back to their common ancestor if more
than 256 roots remain AND the original manifest exceeds 10,000 paths. Otherwise
retain the original list when compaction is ineffective, avoiding a broad scan
for a modest sparse change. Use parents only when at least fourfold smaller.
This bounds large-list Git pathspec matching without a new trie or
persistent index. Root staging can scan unrelated files; validate its actual
delta against the original manifest, which remains the authoritative receipt.

Stage broad roots into a temporary copy of the index using the exclusively created
normal index.lock as the alternate index (Git uses index.lock.lock internally).
Reset this alternate index, never the live index, on resumed calls. Hold index.lock
while preparing, validating and installing this index. Reject any staged path
outside the original manifest (component-boundary prefix matching). Permit a
deleted ancestor blob only when an authorized added descendant requires its
replacement by a directory. Validate before atomically replacing the real index;
on ordinary failure clean temporary files and release only our lock. On process
death standard Git lock recovery applies; do not add a background recovery system.
Existing branch/base/checkout/unstaged checks and original manifest digest stay.

Tests: native-tree equivalence for edits, deletes, additions, ignored files,
symlinks, modes, literal/unusual names, directories and file/directory replacement;
unauthorized sibling/prefix-neighbor changes rejected with index unchanged;
lock contention; large manifest selection and bounded pathspec count; existing
fresh/resumed publication tests. Run official basic and full harness suites.
Repeat the large benchmark with the actual implementation, then remove all owned
remote scratch artifacts and verify the pre-existing volume entries remain.

Independent reviews before and after implementation must challenge complexity,
overengineering, performance regressions and unnecessary rare-case handling.
