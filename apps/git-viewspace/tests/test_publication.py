"""Fresh-operation proofs are local; resumed/individual operations retain validation."""

import shutil
import unittest
from pathlib import Path
from unittest import mock

from git_viewspace import submodules
from git_viewspace.errors import GitViewspaceError
from git_viewspace.git import Git

from tests.test_stage2 import Stage2Test, _FakeSync
from tests.test_submodules import SubmoduleTests


class PublicationTests(unittest.TestCase):
    def fixture(self, *, omitted=False, noop=False):
        fixture = Stage2Test()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        context, daemon = fixture._started_real_git_view_daemon()
        daemon.view_init_and_start("work", "overlay", from_branch="main", new_branch="result")
        helper = _FakeSync()
        helper.report.update(git_pathspecs=[] if omitted or noop else ["created.txt"], whiteouts=[])
        helper.report["counts"].update(whiteouts=0, regulars=0 if noop else 1)

        def replay(_upper, checkout):
            if not noop:
                (Path(checkout) / "created.txt").write_text("created\n")
            return {**helper.report, "mode": "direct"}

        helper.replay = replay
        daemon.sync = helper
        return context, daemon

    def test_fresh_and_resumed_advance_equivalent_with_fewer_git_commands(self):
        counts = {}
        trees = []
        for mode in ("fresh-cold", "fresh", "resumed-sync", "resumed-stage", "individual"):
            with self.subTest(mode=mode):
                context, daemon = self.fixture()
                base = context.repository_facts.head
                submodules._entries_at.cache_clear()
                if mode != "fresh-cold":
                    submodules.entries(context.repository, base)
                commands = []
                original = Git.run

                def counted(
                    instance, args, *rest, _commands=commands, _original=original, **kwargs
                ):
                    _commands.append(tuple(str(part) for part in args))
                    return _original(instance, args, *rest, **kwargs)

                with mock.patch.object(Git, "run", counted):
                    if not mode.startswith("fresh"):
                        daemon.view_stop("work", report=False)
                        daemon.view_sync("work", report=False)
                    if mode in {"resumed-stage", "individual"}:
                        daemon.view_stage("work", report=False)
                    if mode == "individual":
                        daemon.view_commit("work", "publication", report=False)
                        daemon.view_clean("work", report=False)
                        daemon.view_switch("work", use_next=True, report=False)
                        daemon.view_start("work")
                    else:
                        daemon.view_advance("work", "publication")
                counts[mode] = {
                    "cache": "cold" if mode == "fresh-cold" else "base-primed",
                    "commands": len(commands),
                    "resets": sum("reset" in args for args in commands),
                    "index_reads": sum("write-tree" in args for args in commands),
                }
                commit = daemon.git.resolve_local_branch(context.repository, "result")
                trees.append(daemon.git.tree_oid(context.repository, commit))
                self.assertEqual(
                    daemon.git._repo(context.repository, ("show", f"{commit}:created.txt")).stdout,
                    b"created\n",
                )
                self.assertEqual(
                    daemon.git._repo(
                        context.repository, ("rev-list", "--parents", "-n", "1", commit)
                    )
                    .stdout.decode()
                    .split(),
                    [commit, base],
                )
                self.assertIsNone(daemon.store.repository_operation)
        self.assertEqual(len(set(trees)), 1)
        self.assertEqual(counts["fresh"]["resets"], 0)
        self.assertEqual(counts["fresh"]["index_reads"], 2)
        self.assertEqual(counts["resumed-sync"]["resets"], 1)
        self.assertEqual(counts["resumed-sync"]["index_reads"], 3)
        self.assertEqual(counts["resumed-stage"]["index_reads"], 4)
        self.assertEqual(counts["individual"]["index_reads"], 5)  # Separate clean verifies again.
        self.assertLess(counts["fresh"]["commands"], counts["resumed-sync"]["commands"])
        print("publication Git command counts:", counts)

    def test_fresh_noop_and_manifest_completeness(self):
        context, daemon = self.fixture(noop=True)
        result = daemon.view_advance("work", "no-op")
        self.assertEqual(result["view"]["commit"], context.repository_facts.head)
        context, daemon = self.fixture(omitted=True)
        with self.assertRaises(GitViewspaceError) as caught:
            daemon.view_advance("work", "omitted file")
        self.assertEqual(caught.exception.code, "INCOMPLETE_SYNC_MANIFEST")
        self.assertEqual(
            daemon.git.resolve_local_branch(context.repository, "result"),
            context.repository_facts.head,
        )

    def test_resumed_stage_and_commit_refuse_stale_index(self):
        for resume in ("stage", "commit"):
            with self.subTest(resume=resume):
                context, daemon = self.fixture()
                daemon.view_stop("work", report=False)
                daemon.view_sync("work", report=False)
                if resume == "commit":
                    daemon.view_stage("work", report=False)
                (context.repository / "unrelated.txt").write_text("outside transition")
                daemon.git._repo(context.repository, ("add", "unrelated.txt"))
                with self.assertRaises(GitViewspaceError) as caught:
                    daemon.view_advance("work", "must refuse")
                self.assertEqual(
                    caught.exception.code, "STALE_INDEX" if resume == "stage" else "INDEX_DIVERGED"
                )

    def test_fresh_sync_still_checks_checkout_before_staging(self):
        context, daemon = self.fixture()
        original = daemon.view_sync

        def drift(name, **kwargs):
            result = original(name, **kwargs)
            daemon.git._repo(context.repository, ("checkout", "-b", "wrong-branch"))
            return result

        with (
            mock.patch.object(daemon, "view_sync", side_effect=drift),
            mock.patch.object(
                daemon.git, "stage_manifest", wraps=daemon.git.stage_manifest
            ) as stage,
            self.assertRaises(GitViewspaceError) as caught,
        ):
            daemon.view_advance("work", "must not stage")
        self.assertEqual(caught.exception.code, "STALE_SYNC")
        stage.assert_not_called()
        self.assertEqual(
            daemon.git.index_tree(context.repository), daemon.store.get("work").sync["base_tree"]
        )

    def test_metadata_cache_exact_identity_independent_results_and_no_availability_cache(self):
        fixture = SubmoduleTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        submodules._entries_at.cache_clear()
        self.addCleanup(submodules._entries_at.cache_clear)
        with mock.patch.object(
            submodules, "_read_entries", wraps=submodules._read_entries
        ) as reads:
            first = submodules.entries(fixture.repo, fixture.snapshot)
            first["code/library"]["url"] = "corrupted"
            self.assertEqual(
                submodules.entries(fixture.repo / ".", fixture.snapshot)["code/library"]["url"],
                str(fixture.code),
            )
            self.assertEqual(reads.call_count, 1)
            other = fixture.base / "copy"
            shutil.copytree(fixture.repo, other)
            submodules.entries(other, fixture.snapshot)
            self.assertEqual(reads.call_count, 2)
            for _ in range(2):
                submodules.entries(fixture.repo, "HEAD")
            self.assertEqual(reads.call_count, 4)
            fixture.git(
                fixture.repo,
                "update-index",
                "--cacheinfo",
                f"160000,{fixture.second},code/library",
            )
            fixture.git(fixture.repo, "commit", "-m", "new pin")
            new = fixture.git(fixture.repo, "rev-parse", "HEAD")
            self.assertEqual(
                submodules.entries(fixture.repo, new)["code/library"]["commit"], fixture.second
            )
            self.assertEqual(
                submodules.entries(fixture.repo, fixture.snapshot)["code/library"]["commit"],
                fixture.first,
            )
            for _ in range(2):
                with self.assertRaises(GitViewspaceError):
                    submodules.entries(fixture.repo, "0" * 40)
            self.assertEqual(reads.call_count, 7)  # No error cache.
        submodules.prepare(fixture.repo, fixture.snapshot, "code/library")
        submodules.require_available(fixture.repo, fixture.snapshot)
        store = submodules.store_path(fixture.repo, str(fixture.code))
        shutil.rmtree(store)
        with self.assertRaises(GitViewspaceError) as caught:
            submodules.require_available(fixture.repo, fixture.snapshot)
        self.assertEqual(caught.exception.code, "SUBMODULE_NOT_PREPARED")
        with mock.patch.object(submodules, "_read_entries", return_value={}):
            for index in range(140):
                submodules.entries(fixture.repo, f"{index:040x}")
        self.assertEqual(submodules._entries_at.cache_info().currsize, 128)
