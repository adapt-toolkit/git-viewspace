# git-viewspace-fs

## Decoded content cache

Each filesystem process shares a 256 MiB decoded-content LRU across its views, keyed by
owning repository and blob ID. Regular-file size/type queries use object headers rather
than loading contents. Repeated range reads reuse resident immutable bytes.

The budget limits retained payload, not total RSS or transient allocations. Oversized
files are read without retention and do not evict useful entries. Empty files are not
retained. Cache state disappears with the process. Python callers can pass
`content_cache_bytes=0` to `GitOperations` or `GitViewspaceFS` to disable retention.
`cache_stats()` exposes hit/miss/eviction and decoded-byte counters; the existing optional
instrumentation log includes these counters without file contents.

Standalone startup accepts `--content-cache-bytes N`, taking precedence over
`GIT_VIEWSPACE_FS_CONTENT_CACHE_BYTES`. Zero disables retention; invalid/negative values fail
before mounting. Under git-viewspace, set that environment variable before starting the daemon.
The supervisor snapshots it once and forwards an explicit argument through sudo, keeping
the same limit across supervised restarts. A changed limit requires a daemon restart.

Top-level registered gitlinks expose prepared immutable module trees through
the manager's local object stores. See [submodules](../../SUBMODULES.md). Reads never fetch.

git-viewspace-fs is an internal git-viewspace component derived from RepoFS 0.2.6
(upstream commit `14b37ecb91fb86cdbe128f78d5b75d9cc65cab31`). It exposes selected,
read-only Git commit trees through FUSE under `/views`.

RepoFS was authored by Vitalis Salis and Diomidis Spinellis and is available at
<https://github.com/AUEB-BALab/RepoFS>. git-viewspace-fs retains its Apache License
2.0 licensing and the original copyright notices.

Run it with a repository, mount point, and JSON configuration:

```sh
git-viewspace-fs REPOSITORY MOUNT \
  --config CONFIG.json \
  --workload-uid UID \
  --workload-gid GID \
  --root-uuid ROOT-UUID \
  --attempt-token ATTEMPT-TOKEN
```

The numeric workload UID and GID are required, non-root, process-wide values.
git-viewspace-fs reports them for every entry, independently of the FUSE caller. It
reports directories and Git executable files as `0755`, ordinary files as
`0644`, and symbolic links with normal Linux symlink modes. The mount profile
is fixed to read-only, `allow_other`, `default_permissions`, and
`negative_timeout=0`. The root UUID and attempt token appear in the filesystem
source so a controller can distinguish the expected mount from stale residue.
Both source components use at most 128 ASCII letters, digits, dots,
underscores, or hyphens and must begin with a letter or digit.

Stage 0 can additionally pass `--instrumentation-log TRACE.jsonl`. git-viewspace-fs
then appends ordered, source-identified callback and Git-operation events with
cumulative counters. The path must already be an empty, single-link,
owner-writable regular file owned by the configured workload UID/GID; git-viewspace-fs neither
creates it nor follows a final symlink. The optional log is kept outside both
the repository and mount tree. It allows the feasibility harness to
distinguish mount startup from demand-driven tree lookups without privileged
tracing.

The configuration maps one-component view names to exact, full, lowercase
commit object IDs. No other parameters are currently accepted:

```json
{
  "stable": {"commit": "0123456789abcdef0123456789abcdef01234567"}
}
```

An unpinned name reloads the whole file on every lookup. A snapshot is accepted
only when all names, parameters, and commits are valid. Missing, unreadable, or
invalid configuration behaves like an undefined name. The first successful
lookup pins the name and its complete parameters for the lifetime of the
process; later file changes do not alter or remove that view. Directory listing
does not itself pin names.

Resolved commits and tree entries for pinned immutable commit IDs use bounded
in-process LRU caches. The cache retains lightweight entry metadata and
directory-tree objects. The separate decoded-content cache described above retains file contents.

The repository and configuration must resolve outside the mount directory, and
the configuration must be a regular file. The daemon retains the absolute
configuration pathname and reopens it for each dynamic read, so replacing the
file or its symlink is supported; retargeting a path beneath the mount after
startup is an operator error. The obsolete namespace switches and `--no-cache`
option were removed along with their legacy history-enumeration machinery.

Git symbolic-link targets are returned verbatim after strict UTF-8 and NUL
validation. They are not rewritten or confined to their view.
Gitlinks resolve through the manager's explicitly prepared submodule stores.
Unprepared or unsupported registrations fail rather than triggering a network fetch.

For OverlayFS, first resolve and pin a view, then use that specific
`/views/<name>` directory as the lower layer. Do not use the dynamic `/views`
directory itself as a mounted lower layer: Linux does not support changing an
underlying OverlayFS layer while the overlay is mounted.

## Development

git-viewspace-fs supports Python 3.11 or newer and pygit2 1.20.x. Linux also requires
a FUSE 2 compatibility library. From the Git Viewspace source checkout root:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install .
make -C apps/git-viewspace-fs test PYTHON="$PWD/.venv/bin/python"
```

Mount-enabled git-viewspace-fs/OverlayFS validation belongs to git-viewspace's
repository-level Stage 0 harness. The component-local `test` target runs only
the ordinary unit suite.
