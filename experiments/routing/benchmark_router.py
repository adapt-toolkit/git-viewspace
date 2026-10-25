"""Compare existing direct/routed paths; the caller owns disposable mounts/files.

Never point this write benchmark at valuable data. Each root must contain the same
preallocated disposable regular file named data, at least 256 MiB in size.
"""

import argparse
import concurrent.futures
import json
import os
from pathlib import Path
import random
import statistics
import time


def measure(path, operation, size, offsets):
    timings = []
    fd = os.open(path, os.O_RDONLY if operation == "read" else os.O_RDWR)
    try:
        block = b"z" * size
        for offset in offsets:
            start = time.perf_counter_ns()
            if operation == "read":
                assert len(os.pread(fd, size, offset)) == size
            else:
                assert os.pwrite(fd, block, offset) == size
            timings.append(time.perf_counter_ns() - start)
    finally:
        os.close(fd)
    ordered = sorted(timings)
    return dict(
        median_us=statistics.median(ordered) / 1000,
        p95_us=ordered[int(len(ordered) * 0.95)] / 1000,
        p99_us=ordered[int(len(ordered) * 0.99)] / 1000,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--direct", type=Path, required=True)
    parser.add_argument("--routed", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument(
        "--read-only", action="store_true", help="omit writes for immutable Git views"
    )
    parser.add_argument(
        "--files", type=int, default=1, help="use data, data-1, ... for concurrent I/O"
    )
    args = parser.parse_args()
    paths = {"direct": args.direct / "data", "routed": args.routed / "data"}
    length = min(p.stat().st_size for p in paths.values())
    assert length >= 256 * 1024 * 1024
    # Warm both paths without global cache eviction.
    for path in paths.values():
        with path.open("rb") as stream:
            while stream.read(4 * 1024 * 1024):
                pass
    rows = []
    for repeat in range(args.rounds):
        order = ("direct", "routed") if repeat % 2 == 0 else ("routed", "direct")
        for size in (4096, 65536, 1048576, 4194304):
            rng = random.Random(41 + repeat)
            offsets = [
                rng.randrange((length - size) // 4096) * 4096
                for _ in range(2000 if size < 1048576 else 300)
            ]
            for operation in (("read",) if args.read_only else ("read", "write")):
                for name in order:
                    row = dict(
                        repeat=repeat,
                        bytes=size,
                        operation=operation,
                        path=name,
                        **measure(paths[name], operation, size, offsets),
                    )
                    rows.append(row)
        for name in order:
            samples = []
            for _ in range(1000):
                start = time.perf_counter_ns()
                fd = os.open(paths[name], os.O_RDONLY)
                os.fstat(fd)
                os.close(fd)
                samples.append(time.perf_counter_ns() - start)
            rows.append(
                dict(
                    repeat=repeat,
                    path=name,
                    operation="open-stat-close",
                    median_us=statistics.median(samples) / 1000,
                )
            )
        # Concurrent writes use independent files when --files > 1. All fixtures
        # must be disposable; this benchmark never creates production data.
        for operation in (("read",) if args.read_only else ("read", "write")):
            for name in order:
                root = paths[name].parent
                candidates = [
                    root / ("data" if i == 0 else f"data-{i}")
                    for i in range(args.files)
                ]
                assert all(p.stat().st_size >= length for p in candidates)
                with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                    start = time.perf_counter()
                    futures = [
                        pool.submit(
                            measure,
                            candidates[i % len(candidates)],
                            operation,
                            65536,
                            offsets,
                        )
                        for i in range(8)
                    ]
                    for future in futures:
                        future.result()
                    rows.append(
                        dict(
                            repeat=repeat,
                            path=name,
                            operation=(
                                "eight-reader-wall"
                                if operation == "read"
                                else "eight-writer-wall"
                            ),
                            files=args.files,
                            seconds=time.perf_counter() - start,
                        )
                    )
        args.output.write_text(json.dumps(rows, indent=2) + "\n")
        print(f"Completed round {repeat+1}/{args.rounds}", flush=True)


if __name__ == "__main__":
    main()
