#!/usr/bin/env python3
"""Bounded real-mount parallel publication qualification; disposable data only."""

import hashlib
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from git_viewspace.protocol import call


def main():
    base = Path(tempfile.mkdtemp(prefix="cq-", dir=os.environ["VIEWSPACE_WORKSTREAM_SCRATCH"]))
    started = False
    try:
        source, root = base / "source", base / "r"
        source.mkdir()
        environment = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"}

        def git(repository, *args):
            return subprocess.check_output(
                ["git", "-C", str(repository), *args], env=environment, stderr=subprocess.PIPE
            )

        def vm(*args):
            result = subprocess.run(
                [sys.executable, "-m", "git_viewspace", "--root", str(root), "--json", *args],
                env=environment,
                capture_output=True,
                text=True,
                timeout=120,
            )
            if result.returncode:
                raise RuntimeError(f"{args}: {result.stdout} {result.stderr}")
            return json.loads(result.stdout)

        git(source, "init", "-b", "main")
        git(source, "config", "user.name", "Qualification")
        git(source, "config", "user.email", "test@example.invalid")
        files = source / "baseline"
        files.mkdir()
        for index in range(3000):
            (files / f"{index:04}.txt").write_text(f"baseline {index}\n")
        git(source, "add", ".")
        git(source, "commit", "-m", "synthetic baseline")
        baseline_tree = git(source, "rev-parse", "HEAD:baseline").strip()
        baseline_paths = {f"baseline/{index:04}.txt" for index in range(3000)}
        vm(
            "init",
            "--copy",
            str(source),
            "--git-name",
            "Qualification",
            "--git-email",
            "test@example.invalid",
        )
        try:
            vm("start")
            started = True
            names = [f"view{i}" for i in range(4)]
            for name in names:
                vm(
                    "view",
                    "init-and-start",
                    "overlay",
                    name,
                    "--from-branch",
                    "main",
                    "--new-branch",
                    name,
                )
            # CLI owns layout discovery; the protocol test uses the exact manager socket.
            from git_viewspace.root import OnlineRoot

            socket_path = OnlineRoot.load(root).socket_path
            latencies, rounds, timings = [], [], []
            expected_blobs = {}
            expected_commits = {
                name: git(root / "repo", "rev-parse", name).decode().strip() for name in names
            }
            for round_index in range(5):
                for name in names:
                    mount = root / "views" / name / "mnt"
                    (mount / f"{name}.txt").write_text(f"{name} round {round_index}\n")
                    if round_index == 1:
                        payload = os.urandom(1024 * 1024) * 32
                        (mount / f"{name}.bin").write_bytes(payload)
                        expected_blobs[name] = hashlib.sha256(payload).hexdigest()
                started_at = time.monotonic()
                with ThreadPoolExecutor(max_workers=4) as pool:
                    futures = {
                        name: pool.submit(
                            call,
                            socket_path,
                            "view-advance",
                            view=name,
                            message=f"round {round_index}",
                            timeout=120,
                        )
                        for name in names
                    }
                    while any(not future.done() for future in futures.values()):
                        before = time.monotonic()
                        state = call(socket_path, "status", timeout=3)
                        latencies.append(time.monotonic() - before)
                        assert (
                            state["ok"]
                            and state["observation"]["kind"] == "last_consistent_snapshot"
                        ), state
                        time.sleep(0.03)
                    results = {name: future.result() for name, future in futures.items()}
                rounds.append(time.monotonic() - started_at)
                for name, result in results.items():
                    assert result["ok"], result
                    timings.append(result["timing"])
                    commit = result["view"]["commit"]
                    repository = root / "repo"
                    parents = (
                        git(repository, "rev-list", "--parents", "-n", "1", commit).decode().split()
                    )
                    assert parents == [commit, expected_commits[name]], parents
                    expected_commits[name] = commit
                    paths = (
                        git(repository, "ls-tree", "-r", "--name-only", commit)
                        .decode()
                        .splitlines()
                    )
                    expected_paths = baseline_paths | {f"{name}.txt"}
                    if round_index >= 1:
                        expected_paths.add(f"{name}.bin")
                    assert set(paths) == expected_paths
                    assert (
                        git(repository, "rev-parse", f"{commit}:baseline").strip() == baseline_tree
                    )
                    assert (
                        git(repository, "show", f"{commit}:{name}.txt")
                        == f"{name} round {round_index}\n".encode()
                    )
                    mount = root / "views" / name / "mnt"
                    assert (mount / f"{name}.txt").read_text() == f"{name} round {round_index}\n"
                    if round_index >= 1:
                        assert (
                            hashlib.sha256(
                                git(repository, "show", f"{commit}:{name}.bin")
                            ).hexdigest()
                            == expected_blobs[name]
                        )
                        assert (
                            hashlib.sha256((mount / f"{name}.bin").read_bytes()).hexdigest()
                            == expected_blobs[name]
                        )
                print(
                    json.dumps(
                        {"round": round_index, "seconds": rounds[-1], "commits": len(results)}
                    ),
                    flush=True,
                )
            for name in names:
                vm("view", "stop", name)
                vm("view", "destroy", name, "--discard")
            stopped = vm("stop")
            started = False
            assert stopped["mounts_remaining"] == stopped["children_remaining"] == 0
            print(
                json.dumps(
                    {
                        "commits": 20,
                        "baseline_files": 3000,
                        "large_bytes_per_view": 32 * 1024 * 1024,
                        "round_seconds": rounds,
                        "status_samples": len(latencies),
                        "status_p50_seconds": statistics.median(latencies),
                        "status_max_seconds": max(latencies),
                        "max_queue_seconds": max(t["queue_seconds"] for t in timings),
                        "max_service_seconds": max(t["service_seconds"] for t in timings),
                    }
                ),
                flush=True,
            )
        finally:
            if started:
                vm("stop")
                started = False
    finally:
        # Never recursively delete a mounted tree, or hide a failed shutdown's evidence.
        if started or str(base) in Path("/proc/self/mountinfo").read_text():
            raise RuntimeError(f"test teardown incomplete; fixture retained: {base}")
        shutil.rmtree(base)


if __name__ == "__main__":
    main()
