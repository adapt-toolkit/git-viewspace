# Routing layer: initial experiments

The experiment is an original small C program using libfuse 3.17.2's high-level
API and Linux's backing-file ioctls. It is not a derived copy of libfuse's GPL
example. The LGPL shared library is dynamically linked. The probe intentionally
supports one regular data file; it is not the implementation or a general mount.

Host: Linux 6.12.96, CONFIG_FUSE_PASSTHROUGH=y, libfuse 3.17.2, x86-64 Linux VM.
Build headers were extracted into scratch; existing services were not changed.

## Evidence

- Real root-authorized FUSE mount over OverlayFS: passthrough negotiated.
- Random reads/writes completed with **zero userspace read/write callbacks**.
- Full Git-FUSE -> OverlayFS -> routing-FUSE stack mounted successfully.
- Simultaneous reader and writer across OverlayFS copy-up succeeded after sharing
  one kernel backing registration per exposed file. Eight concurrent opens each
  completed 100 disjoint-region write/read checks.
- A naive registration per open produced EIO on the second concurrent open.
  Production must reference-count the shared registration; this is a correctness
  requirement, not an optional cache optimization.
- Ordinary userspace forwarding without passthrough was substantially slower.
  Silent fallback is unsuitable for the feature's performance goal.

Three alternating-order warm-cache rounds, 256 MiB file, random aligned offsets,
3,000 operations at 4/64 KiB and 600 at 1 MiB, Python pread/pwrite, fsync outside
timed batches. Median per-operation latency ratios (routed/direct OverlayFS):

| Block | Passthrough read | Passthrough write | Userspace read | Userspace write |
|---|---:|---:|---:|---:|
| 4 KiB | 1.17 | 1.05 | 14.49 | 13.05 |
| 64 KiB | 1.32 | 1.02 | 5.95 | 4.56 |
| 1 MiB | 1.00 | 0.98 | 2.62 | 3.51 |

These are preliminary VM measurements, not performance guarantees. Raw samples
are alongside this document. Final implementation benchmarks must include more
rounds, latency percentiles, metadata/open overhead, multiple files and concurrency.
No global cache dropping or production workloads were used. Test mounts and the
disposable Git Viewspace daemon were stopped after the experiment.

## Design consequences

Require kernel passthrough for ordinary files in enabled routed views; unsupported
hosts fail clearly while ordinary non-routed views retain their existing behavior.
Use the high-level C/C++ interface rather than writing an inode/FUSE protocol engine.
Set max_backing_stack_depth to FUSE_BACKING_STACKED_OVER for OverlayFS backing.
Special files do not use passthrough or cached data: their operations must reach
the router for backing-identity checks.

References:
- https://docs.kernel.org/filesystems/fuse/fuse-passthrough.html
- https://libfuse.github.io/doxygen/fuse__lowlevel_8h.html
- https://github.com/libfuse/libfuse/blob/fuse-3.17.2/ChangeLog.rst
