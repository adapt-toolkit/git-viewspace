"""Git subprocess boundary and repository qualification."""

import os
import shutil
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .errors import GitViewspaceError


MAX_MERGE_CONFLICT_PATHS = 100
MAX_MERGE_CONFLICT_PATH_BYTES = 64 * 1024
STAGING_ROOT_LIMIT = 256
STAGING_BROAD_THRESHOLD = 10_000


@dataclass(frozen=True)
class RepositoryFacts:
    path: Path
    git_directory: Path
    head: str
    branch: str | None
    clean: bool


class Git:
    def __init__(self, executable=None):
        selected = executable or shutil.which("git")
        if selected is None:
            raise GitViewspaceError("GIT_NOT_FOUND", "Git executable was not found")
        self.executable = str(Path(selected).resolve())
        self._validated_branches = set()
        self._validated_commits = set()

    @staticmethod
    def _environment(*, authentication=False, identity=None):
        environment = os.environ.copy()
        for name in (
            "GIT_DIR",
            "GIT_WORK_TREE",
            "GIT_INDEX_FILE",
            "GIT_OBJECT_DIRECTORY",
            "GIT_ALTERNATE_OBJECT_DIRECTORIES",
            "GIT_COMMON_DIR",
            "GIT_NAMESPACE",
        ):
            environment.pop(name, None)
        if not authentication:
            for name in list(environment):
                if name == "GIT_CONFIG_COUNT" or name.startswith("GIT_CONFIG_KEY_") or name.startswith("GIT_CONFIG_VALUE_"):
                    environment.pop(name, None)
            environment["GIT_CONFIG_NOSYSTEM"] = "1"
            environment["GIT_CONFIG_GLOBAL"] = os.devnull
        environment["LC_ALL"] = "C.UTF-8"
        environment["GIT_OPTIONAL_LOCKS"] = "0"
        if identity is not None:
            name, email = identity
            environment.update(
                {
                    "GIT_AUTHOR_NAME": name,
                    "GIT_AUTHOR_EMAIL": email,
                    "GIT_COMMITTER_NAME": name,
                    "GIT_COMMITTER_EMAIL": email,
                }
            )
        return environment

    def run(
        self,
        arguments,
        *,
        cwd=None,
        input_bytes=None,
        authentication=False,
        identity=None,
        environment_overrides=None,
        check=True,
        timeout=None,
    ):
        command = [self.executable, *map(str, arguments)]
        environment = self._environment(authentication=authentication, identity=identity)
        if environment_overrides:
            environment.update({key: str(value) for key, value in environment_overrides.items()})
        try:
            result = subprocess.run(
                command,
                cwd=cwd,
                input=input_bytes,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment,
                check=False,
                timeout=timeout,
            )
        except OSError as error:
            raise GitViewspaceError("GIT_EXEC_FAILED", "could not execute Git", detail=str(error)) from error
        if check and result.returncode != 0:
            detail = result.stderr.decode("utf-8", "replace").strip()
            raise GitViewspaceError("GIT_FAILED", "Git command failed", detail=detail)
        return result

    def validate_branch_name(self, branch):
        if not isinstance(branch, str) or not branch:
            raise GitViewspaceError("INVALID_BRANCH", "invalid branch name")
        if branch in self._validated_branches:
            return
        result = self.run(("check-ref-format", "--branch", branch), check=False)
        if result.returncode != 0:
            raise GitViewspaceError("INVALID_BRANCH", f"invalid branch name: {branch}")
        self._validated_branches.add(branch)

    def clone(self, source, destination, branch=None):
        arguments = ["clone"]
        if branch is not None:
            arguments.extend(("--branch", branch))
        arguments.extend(("--", source, destination))
        result = self.run(arguments, authentication=True, check=False)
        if result.returncode != 0:
            detail = result.stderr.decode("utf-8", "replace").strip()
            raise GitViewspaceError(
                "GIT_CLONE_FAILED",
                "Git clone failed; configure Git authentication or prepare a clean local checkout and use --move",
                detail=detail,
            )

    def create_empty(self, destination, branch, identity):
        self.run(("-c", "init.templateDir=", "init", "--initial-branch", branch, "--", destination))
        self.run(
            (
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "commit.gpgSign=false",
                "-C",
                destination,
                "commit",
                "--allow-empty",
                "--no-gpg-sign",
                "-m",
                "Initial empty commit",
            ),
            identity=identity,
        )

    def _repo(self, repository, arguments, *, check=True, input_bytes=None):
        return self.run(("-C", repository, *arguments), check=check, input_bytes=input_bytes)

    def _repo_with_environment(
        self,
        repository,
        arguments,
        *,
        environment_overrides,
        input_bytes=None,
        check=True,
    ):
        return self.run(
            ("-c", "core.hooksPath=/dev/null", "-C", repository, *arguments),
            check=check,
            input_bytes=input_bytes,
            environment_overrides=environment_overrides,
        )

    def _config_bool(self, repository, key):
        result = self._repo(repository, ("config", "--bool", "--get", key), check=False)
        if result.returncode == 1:
            return None
        if result.returncode != 0:
            raise GitViewspaceError("INVALID_REPOSITORY", f"cannot read Git setting {key}")
        value = result.stdout.strip()
        if value == b"true":
            return True
        if value == b"false":
            return False
        raise GitViewspaceError("INVALID_REPOSITORY", f"invalid Git setting {key}")

    def _validate_commit_tree(self, repository, commit, *, require_blobs=False):
        tree = self._repo(repository, ("ls-tree", "-r", "-z", commit)).stdout
        blobs = set()
        for record in tree.split(b"\0"):
            if not record:
                continue
            try:
                metadata, encoded_path = record.split(b"\t", 1)
                mode, object_type, object_id = metadata.split(b" ", 2)
                path = encoded_path.decode("utf-8")
            except (ValueError, UnicodeDecodeError) as error:
                raise GitViewspaceError(
                    "UNSUPPORTED_COMMIT",
                    "Git tree contains an invalid or non-UTF-8 entry",
                ) from error
            if any(component == ".git" for component in path.split("/")):
                raise GitViewspaceError("UNSUPPORTED_COMMIT", "Git tree contains a .git component")
            if mode == b"160000":
                if object_type != b"commit":
                    raise GitViewspaceError("UNSUPPORTED_COMMIT", "invalid gitlink type")
                continue
            if mode not in {b"100644", b"100755", b"120000"} or object_type != b"blob":
                raise GitViewspaceError("UNSUPPORTED_COMMIT", f"unsupported Git tree mode at {path}")
            blobs.add(object_id)
            if mode == b"120000":
                target = self._repo(
                    repository,
                    ("cat-file", "blob", object_id.decode("ascii")),
                ).stdout
                try:
                    target.decode("utf-8")
                except UnicodeDecodeError as error:
                    raise GitViewspaceError(
                        "UNSUPPORTED_COMMIT",
                        f"symbolic-link target is not UTF-8: {path}",
                    ) from error
                if b"\0" in target:
                    raise GitViewspaceError(
                        "UNSUPPORTED_COMMIT",
                        f"symbolic-link target contains NUL: {path}",
                    )

        if require_blobs and blobs:
            # Batch header checks avoid decompressing every payload or starting
            # a subprocess per file, while detecting incomplete object stores.
            ordered = sorted(blobs)
            result = self._repo(
                repository, ("cat-file", "--batch-check=%(objectname) %(objecttype)"),
                input_bytes=b"\n".join(ordered) + b"\n",
            ).stdout.splitlines()
            if result != [oid + b" blob" for oid in ordered]:
                raise GitViewspaceError("SUBMODULE_NOT_PREPARED", "module tree references unavailable blobs")

        if b"160000 " in tree:
            from .submodules import entries
            entries(repository, commit, git=self)

    def resolve_commit(self, repository, commitish):
        if (
            not isinstance(commitish, str)
            or not commitish
            or commitish != commitish.strip()
            or any(character in commitish for character in ("\0", "\r", "\n"))
        ):
            raise GitViewspaceError("INVALID_COMMIT", "commit argument is invalid")
        result = self._repo(
            repository,
            ("rev-parse", "--verify", "--end-of-options", f"{commitish}^{{commit}}"),
            check=False,
        )
        if result.returncode != 0:
            raise GitViewspaceError("COMMIT_NOT_FOUND", f"cannot resolve commit: {commitish}")
        commit = result.stdout.decode("ascii", "strict").strip()
        if len(commit) != 40 or any(character not in "0123456789abcdef" for character in commit):
            raise GitViewspaceError("INVALID_COMMIT", "Git returned an invalid commit identity")
        if commit not in self._validated_commits:
            self._validate_commit_tree(repository, commit)
            self._validated_commits.add(commit)
        return commit

    def resolve_local_branch(self, repository, branch):
        self.validate_branch_name(branch)
        result = self._repo(
            repository,
            ("rev-parse", "--verify", "--end-of-options", f"refs/heads/{branch}^{{commit}}"),
            check=False,
        )
        if result.returncode != 0:
            raise GitViewspaceError("BRANCH_NOT_FOUND", f"local branch does not exist: {branch}")
        commit = result.stdout.decode("ascii", "strict").strip()
        if len(commit) != 40 or any(character not in "0123456789abcdef" for character in commit):
            raise GitViewspaceError("INVALID_BRANCH", "Git returned an invalid branch tip")
        return commit

    def resolve_direct_local_branch(self, repository, branch):
        """Resolve one local branch while rejecting a symbolic branch ref."""

        self.validate_branch_name(branch)
        reference = f"refs/heads/{branch}"
        symbolic = self._repo(repository, ("symbolic-ref", "-q", reference), check=False)
        if symbolic.returncode == 0:
            raise GitViewspaceError(
                "SYMBOLIC_BRANCH_UNSUPPORTED",
                f"local branch is symbolic rather than direct: {branch}",
            )
        if symbolic.returncode != 1:
            raise GitViewspaceError(
                "GIT_FAILED",
                "could not inspect local branch representation",
                detail=symbolic.stderr.decode("utf-8", "replace").strip(),
            )
        return self.resolve_local_branch(repository, branch)

    def create_local_branch(self, repository, branch, parent_branch):
        """Create one local branch at the exact resolved tip of a local parent.

        ``update-ref`` compares against the all-zero object ID, so concurrent creators cannot
        replace an existing direct ref. The explicit symbolic-ref check also treats a dangling
        symbolic branch as occupied instead of allowing ``update-ref`` to create its target. A
        collision is a pre-effect refusal; any other Git failure keeps its generic infrastructure
        classification.
        """
        self.validate_branch_name(branch)
        commit = self.resolve_direct_local_branch(repository, parent_branch)
        reference = f"refs/heads/{branch}"
        symbolic = self._repo(
            repository,
            ("symbolic-ref", "-q", reference),
            check=False,
        )
        if symbolic.returncode == 0:
            raise GitViewspaceError(
                "BRANCH_EXISTS",
                f"local branch already exists in symbolic form: {branch}",
            )
        if symbolic.returncode != 1:
            detail = symbolic.stderr.decode("utf-8", "replace").strip()
            raise GitViewspaceError("GIT_FAILED", "could not inspect local branch", detail=detail)
        result = self._repo(
            repository,
            ("update-ref", reference, commit, "0" * 40),
            check=False,
        )
        if result.returncode == 0:
            return commit
        existing = self._repo(
            repository,
            ("rev-parse", "--verify", "--end-of-options", reference),
            check=False,
        )
        if existing.returncode == 0:
            raise GitViewspaceError("BRANCH_EXISTS", f"local branch already exists: {branch}")
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise GitViewspaceError("GIT_FAILED", "could not create local branch", detail=detail)

    def resolve_exact_commit(self, repository, commit):
        """Resolve one exact lowercase SHA-1 whose object is itself a commit."""

        if (
            not isinstance(commit, str)
            or len(commit) != 40
            or any(character not in "0123456789abcdef" for character in commit)
        ):
            raise GitViewspaceError(
                "INVALID_COMMIT",
                "commit must be exactly 40 lowercase hexadecimal characters",
            )
        resolved = self._repo(repository, ("cat-file", "-t", commit), check=False)
        if resolved.returncode != 0:
            raise GitViewspaceError("COMMIT_NOT_FOUND", f"commit does not exist: {commit}")
        object_type = resolved.stdout.strip()
        if object_type != b"commit":
            if object_type in {b"blob", b"tree", b"tag"}:
                raise GitViewspaceError("INVALID_COMMIT", "object is not a commit")
            raise GitViewspaceError("GIT_FAILED", "Git returned an invalid object type")
        return commit

    def pin_local_branch(self, repository, branch, commit):
        """Create one absent direct branch at an exact commit, idempotently.

        ``update-ref`` compares against the all-zero object ID. Concurrent identical callers
        therefore converge on one created ref, while a different existing tip is never replaced.
        """

        self.validate_branch_name(branch)
        commit = self.resolve_exact_commit(repository, commit)
        reference = f"refs/heads/{branch}"
        symbolic = self._repo(repository, ("symbolic-ref", "-q", reference), check=False)
        if symbolic.returncode == 0:
            raise GitViewspaceError(
                "SYMBOLIC_BRANCH_UNSUPPORTED",
                f"local branch is symbolic rather than direct: {branch}",
            )
        if symbolic.returncode != 1:
            raise GitViewspaceError(
                "GIT_FAILED",
                "could not inspect local branch representation",
                detail=symbolic.stderr.decode("utf-8", "replace").strip(),
            )

        updated = self._repo(
            repository,
            ("update-ref", "--no-deref", reference, commit, "0" * 40),
            check=False,
        )
        if updated.returncode == 0:
            return {"branch": branch, "commit": commit, "created": True}

        # A concurrent creator may have won the compare-and-update. Re-read the exact ref and
        # classify only the stable direct outcomes; any unexplained failure remains a Git error.
        symbolic = self._repo(repository, ("symbolic-ref", "-q", reference), check=False)
        if symbolic.returncode == 0:
            raise GitViewspaceError(
                "SYMBOLIC_BRANCH_UNSUPPORTED",
                f"local branch is symbolic rather than direct: {branch}",
            )
        if symbolic.returncode != 1:
            raise GitViewspaceError(
                "GIT_FAILED",
                "could not inspect local branch representation",
                detail=symbolic.stderr.decode("utf-8", "replace").strip(),
            )
        existing = self._repo(
            repository,
            ("show-ref", "--verify", "--hash", reference),
            check=False,
        )
        if existing.returncode == 0:
            existing_commit = self._oid_from_result(existing, "local branch tip")
            if existing_commit == commit:
                return {"branch": branch, "commit": commit, "created": False}
            raise GitViewspaceError(
                "BRANCH_DIVERGED",
                f"local branch points to another commit: {branch}",
                data={
                    "branch": branch,
                    "expected_commit": commit,
                    "actual_commit": existing_commit,
                },
            )
        detail = updated.stderr.decode("utf-8", "replace").strip()
        raise GitViewspaceError("GIT_FAILED", "could not pin local branch", detail=detail)

    def _is_ancestor(self, repository, ancestor, descendant):
        result = self._repo(
            repository,
            ("merge-base", "--is-ancestor", ancestor, descendant),
            check=False,
        )
        if result.returncode == 0:
            return True
        if result.returncode == 1:
            return False
        raise GitViewspaceError(
            "GIT_FAILED",
            "could not compare branch ancestry",
            detail=result.stderr.decode("utf-8", "replace").strip(),
        )

    def validate_merge_request(
        self,
        attempt_branch,
        parent_branch,
        expected_attempt_tip,
        message,
        checked_out_destination,
    ):
        """Validate the scalar merge boundary before repository effects."""

        self.validate_branch_name(attempt_branch)
        self.validate_branch_name(parent_branch)
        if attempt_branch == parent_branch:
            raise GitViewspaceError("INVALID_ARGUMENT", "attempt and parent branches must differ")
        if (
            not isinstance(expected_attempt_tip, str)
            or len(expected_attempt_tip) != 40
            or any(character not in "0123456789abcdef" for character in expected_attempt_tip)
        ):
            raise GitViewspaceError("INVALID_COMMIT", "expected attempt tip must be a lowercase SHA-1")
        if not isinstance(message, str) or not message or "\0" in message:
            raise GitViewspaceError("INVALID_MESSAGE", "merge message must be nonempty and contain no NUL")
        if checked_out_destination not in {"refuse", "detach_if_clean"}:
            raise GitViewspaceError(
                "INVALID_ARGUMENT",
                "checked-out-destination policy is invalid",
            )
        return message.encode("utf-8")

    def _resolve_merge_branch(self, repository, branch, *, attempt):
        """Resolve one direct merge branch with its role-specific absence error."""

        try:
            return self.resolve_direct_local_branch(repository, branch)
        except GitViewspaceError as error:
            if error.code != "BRANCH_NOT_FOUND":
                raise
            role = "attempt" if attempt else "parent"
            raise GitViewspaceError(
                "ATTEMPT_BRANCH_NOT_FOUND" if attempt else "PARENT_BRANCH_NOT_FOUND",
                f"{role} branch does not exist: {branch}",
                data={"branch": branch},
            ) from error

    @staticmethod
    def _parent_tip_moved_data(parent_branch, expected_parent_tip, actual_parent_tip):
        return {
            "parent_branch": parent_branch,
            "expected_parent_tip": expected_parent_tip,
            "actual_parent_tip": actual_parent_tip,
        }

    def _detach_parent_checkout_if_clean(self, repository, parent_branch, parent_tip):
        """Detach an exact clean parent checkout without rewriting its files or index."""

        checkout_head, checkout_branch = self.checkout_position(repository)
        if checkout_branch != parent_branch:
            return False
        if checkout_head != parent_tip:
            raise GitViewspaceError(
                "PARENT_TIP_MOVED",
                "parent branch moved before its checkout could be detached",
                data=self._parent_tip_moved_data(
                    parent_branch,
                    parent_tip,
                    checkout_head,
                ),
            )
        if self.checkout_status(repository):
            raise GitViewspaceError(
                "DIRTY_REPOSITORY",
                "parent branch checkout must be clean before detachment",
                data={"parent_branch": parent_branch, "parent_tip": parent_tip},
            )
        detached = self._repo(
            repository,
            ("update-ref", "--no-deref", "HEAD", parent_tip, parent_tip),
            check=False,
        )
        if detached.returncode != 0:
            raise GitViewspaceError(
                "GIT_FAILED",
                "Git could not detach the clean parent checkout",
                detail=detached.stderr.decode("utf-8", "replace").strip(),
            )
        if self.checkout_position(repository) != (parent_tip, None):
            raise GitViewspaceError(
                "GIT_FAILED",
                "detached checkout does not retain the exact parent tip",
            )
        if self.checkout_status(repository):
            raise GitViewspaceError(
                "GIT_FAILED",
                "detaching the parent checkout changed its index or worktree",
            )
        return True

    @staticmethod
    def _merge_conflict_data(output, *, attempt_branch, attempt_tip, parent_branch, parent_tip):
        fields = output.split(b"\0")
        if fields and fields[-1] == b"":
            fields.pop()
        if not fields:
            raise GitViewspaceError("GIT_FAILED", "Git omitted the conflicted merge tree")
        Git._oid_from_bytes(fields[0], "merge tree")
        paths = []
        retained_bytes = 0
        for encoded in fields[1:]:
            try:
                path = encoded.decode("utf-8", "strict")
            except UnicodeDecodeError as error:
                raise GitViewspaceError("GIT_FAILED", "Git returned a non-UTF-8 conflict path") from error
            path_bytes = len(encoded)
            if (
                len(paths) < MAX_MERGE_CONFLICT_PATHS
                and path_bytes <= MAX_MERGE_CONFLICT_PATH_BYTES
                and retained_bytes + path_bytes <= MAX_MERGE_CONFLICT_PATH_BYTES
            ):
                paths.append(path)
                retained_bytes += path_bytes
        return {
            "attempt_branch": attempt_branch,
            "attempt_tip": attempt_tip,
            "parent_branch": parent_branch,
            "parent_tip": parent_tip,
            "conflict_path_count": len(fields) - 1,
            "conflict_paths_rr": paths,
            "conflict_paths_truncated": len(paths) != len(fields) - 1,
        }

    def merge_local_branches(
        self,
        repository,
        attempt_branch,
        parent_branch,
        expected_attempt_tip,
        message,
        identity,
        before_merge,
        checked_out_destination="refuse",
    ):
        """Create one two-parent merge and atomically advance only the parent ref.

        The merge is computed directly from commit trees. Under ``detach_if_clean``, a managed
        checkout attached to the parent is detached at its exact clean tip without rewriting the
        index or worktree. Unreferenced objects may remain after a conflict or failed
        compare-and-update.
        """

        message_bytes = self.validate_merge_request(
            attempt_branch,
            parent_branch,
            expected_attempt_tip,
            message,
            checked_out_destination,
        )

        attempt_tip = self._resolve_merge_branch(
            repository,
            attempt_branch,
            attempt=True,
        )
        if attempt_tip != expected_attempt_tip:
            raise GitViewspaceError(
                "ATTEMPT_TIP_MISMATCH",
                "attempt branch tip differs from the required commit",
                data={
                    "attempt_branch": attempt_branch,
                    "actual_attempt_tip": attempt_tip,
                    "expected_attempt_tip": expected_attempt_tip,
                },
            )
        parent_tip = self._resolve_merge_branch(
            repository,
            parent_branch,
            attempt=False,
        )
        if self._is_ancestor(repository, attempt_tip, parent_tip):
            return {
                "attempt_branch": attempt_branch,
                "attempt_tip": attempt_tip,
                "parent_branch": parent_branch,
                "parent_tip_before": parent_tip,
                "parent_tip_after": parent_tip,
                "idempotent": True,
            }

        # The daemon supplies its cheap view-claim check here so idempotent replay remains
        # read-only while a new merge proves policy immediately before creating Git objects.
        before_merge()
        if (
            checked_out_destination == "refuse"
            and self.checkout_branch(repository) == parent_branch
        ):
            raise GitViewspaceError(
                "BRANCH_CHECKED_OUT",
                "parent branch is the managed repository checkout",
                data={"parent_branch": parent_branch, "parent_tip": parent_tip},
            )

        merged = self._repo(
            repository,
            (
                "merge-tree",
                "--write-tree",
                "--name-only",
                "-z",
                "--no-messages",
                parent_tip,
                attempt_tip,
            ),
            check=False,
        )
        if merged.returncode == 1:
            raise GitViewspaceError(
                "BRANCH_MERGE_CONFLICT",
                "attempt branch conflicts with its parent",
                data=self._merge_conflict_data(
                    merged.stdout,
                    attempt_branch=attempt_branch,
                    attempt_tip=attempt_tip,
                    parent_branch=parent_branch,
                    parent_tip=parent_tip,
                ),
            )
        if merged.returncode != 0:
            raise GitViewspaceError(
                "GIT_FAILED",
                "Git could not compute the branch merge",
                detail=merged.stderr.decode("utf-8", "replace").strip(),
            )
        merged_fields = merged.stdout.split(b"\0")
        if len(merged_fields) != 2 or merged_fields[1] != b"":
            raise GitViewspaceError("GIT_FAILED", "Git returned invalid successful merge output")
        merge_tree = self._oid_from_bytes(merged_fields[0], "merge tree")
        committed = self.run(
            (
                "-c",
                "commit.gpgSign=false",
                "-C",
                repository,
                "commit-tree",
                merge_tree,
                "-p",
                parent_tip,
                "-p",
                attempt_tip,
                "-F",
                "-",
            ),
            input_bytes=message_bytes,
            identity=identity,
        )
        merge_commit = self._oid_from_result(committed, "merge commit")
        actual_parent_tip = self._resolve_merge_branch(
            repository,
            parent_branch,
            attempt=False,
        )
        if actual_parent_tip != parent_tip:
            raise GitViewspaceError(
                "PARENT_TIP_MOVED",
                "parent branch moved before the merge could be published",
                data=self._parent_tip_moved_data(
                    parent_branch,
                    parent_tip,
                    actual_parent_tip,
                ),
            )
        if checked_out_destination == "detach_if_clean":
            self._detach_parent_checkout_if_clean(
                repository,
                parent_branch,
                parent_tip,
            )
        updated = self._repo(
            repository,
            (
                "update-ref",
                f"refs/heads/{parent_branch}",
                merge_commit,
                parent_tip,
            ),
            check=False,
        )
        if updated.returncode != 0:
            try:
                actual_parent_tip = self._resolve_merge_branch(
                    repository,
                    parent_branch,
                    attempt=False,
                )
            except GitViewspaceError as error:
                raise GitViewspaceError(
                    "GIT_FAILED",
                    "parent branch disappeared while publishing the merge",
                    detail=updated.stderr.decode("utf-8", "replace").strip(),
                ) from error
            if actual_parent_tip == parent_tip:
                raise GitViewspaceError(
                    "GIT_FAILED",
                    "Git could not publish the branch merge",
                    detail=updated.stderr.decode("utf-8", "replace").strip(),
                )
            raise GitViewspaceError(
                "PARENT_TIP_MOVED",
                "parent branch moved before the merge could be published",
                detail=updated.stderr.decode("utf-8", "replace").strip(),
                data={
                    **self._parent_tip_moved_data(
                        parent_branch,
                        parent_tip,
                        actual_parent_tip,
                    ),
                },
            )
        return {
            "attempt_branch": attempt_branch,
            "attempt_tip": attempt_tip,
            "parent_branch": parent_branch,
            "parent_tip_before": parent_tip,
            "parent_tip_after": merge_commit,
            "idempotent": False,
        }

    @staticmethod
    def _oid_from_bytes(payload, label):
        try:
            value = payload.decode("ascii", "strict").strip()
        except UnicodeError as error:
            raise GitViewspaceError("GIT_FAILED", f"Git returned an invalid {label}") from error
        if len(value) != 40 or any(character not in "0123456789abcdef" for character in value):
            raise GitViewspaceError("GIT_FAILED", f"Git returned an invalid {label}")
        return value

    @staticmethod
    def _oid_from_result(result, label):
        return Git._oid_from_bytes(result.stdout, label)

    def tree_oid(self, repository, commit):
        result = self._repo(
            repository,
            ("rev-parse", "--verify", "--end-of-options", f"{commit}^{{tree}}"),
        )
        return self._oid_from_result(result, "tree identity")

    def index_tree(self, repository):
        return self._oid_from_result(
            self._repo(repository, ("write-tree",)),
            "index tree identity",
        )

    def checkout_status(self, repository):
        return self._repo(
            repository,
            ("status", "--porcelain=v1", "-z", "--untracked-files=all"),
        ).stdout

    def unstaged_paths(self, repository):
        tracked = self._repo(
            repository,
            (
                "diff",
                "--name-only",
                "-z",
                "--no-renames",
                "--no-ext-diff",
                "--no-textconv",
                "--",
            ),
        ).stdout
        untracked = self._repo(repository, ("ls-files", "--others", "-z", "--")).stdout
        paths = []
        for encoded in tracked.split(b"\0") + untracked.split(b"\0"):
            if encoded:
                try:
                    paths.append(encoded.decode("utf-8", "strict"))
                except UnicodeDecodeError as error:
                    raise GitViewspaceError("UNSUPPORTED_CHECKOUT", "checkout path is not UTF-8") from error
        return tuple(sorted(set(paths)))

    def validate_redirect_sources(self, repository, base_commit, redirects):
        for redirect in redirects:
            source = redirect["source"]
            result = self._repo(
                repository,
                ("cat-file", "-t", f"{base_commit}:{source}"),
                check=False,
            )
            if result.returncode != 0 or result.stdout != b"tree\n":
                raise GitViewspaceError(
                    "UNSUPPORTED_OVERLAY_ARTIFACT",
                    f"redirect source is not a directory in the base commit: {source}",
                )

    def checkout_branch(self, repository):
        """Return the checked-out local branch, or ``None`` for detached HEAD."""

        symbolic = self._repo(repository, ("symbolic-ref", "-q", "HEAD"), check=False)
        if symbolic.returncode == 0:
            return symbolic.stdout.decode("utf-8", "strict").strip().removeprefix("refs/heads/")
        if symbolic.returncode == 1:
            return None
        raise GitViewspaceError("GIT_FAILED", "cannot inspect checkout branch")

    def checkout_position(self, repository):
        head = self._oid_from_result(
            self._repo(repository, ("rev-parse", "--verify", "HEAD^{commit}")),
            "checkout HEAD",
        )
        return head, self.checkout_branch(repository)

    def prepare_sync_checkout(self, repository, branch, base_commit):
        if self.resolve_local_branch(repository, branch) != base_commit:
            raise GitViewspaceError("BRANCH_DIVERGED", f"owned branch tip differs from sync base: {branch}")
        git_directory = Path(repository) / ".git"
        for marker in (
            "MERGE_HEAD",
            "CHERRY_PICK_HEAD",
            "REVERT_HEAD",
            "REBASE_HEAD",
            "BISECT_LOG",
            "rebase-apply",
            "rebase-merge",
            "sequencer",
        ):
            if (git_directory / marker).exists():
                raise GitViewspaceError("GIT_OPERATION_IN_PROGRESS", f"Git operation is in progress: {marker}")
        prefix = ("-c", "core.hooksPath=/dev/null", "-C", repository)
        self.run((*prefix, "checkout", "--force", branch))
        self.run((*prefix, "clean", "-ffdx"))
        head, checkout_branch = self.checkout_position(repository)
        if head != base_commit or checkout_branch != branch:
            raise GitViewspaceError("CHECKOUT_DIVERGED", "checkout does not match the sync base")
        base_tree = self.tree_oid(repository, base_commit)
        if self.index_tree(repository) != base_tree:
            raise GitViewspaceError("CHECKOUT_DIVERGED", "checkout reset did not produce the base tree")
        return base_tree

    @staticmethod
    def _staging_roots(manifest):
        """Compact execution paths only; the original manifest remains authoritative."""
        if len(manifest) <= STAGING_ROOT_LIMIT:
            return manifest
        parents = {path.rpartition("/")[0] or "." for path in manifest}
        roots = []
        for path in sorted(parents):
            if "." in parents or any(
                path[:i] in parents for i, char in enumerate(path) if char == "/"
            ):
                continue
            roots.append(path)
        if "." in parents:
            roots = ["."]
        if len(roots) <= STAGING_ROOT_LIMIT and len(roots) * 4 <= len(manifest):
            return roots
        if len(manifest) > STAGING_BROAD_THRESHOLD:
            return [os.path.commonpath(manifest) or "."]
        return manifest

    @staticmethod
    def _validate_staging_delta(delta, manifest):
        allowed = set(manifest)

        def ancestors(path):
            return (path[:i] for i, char in enumerate(path) if char == "/")

        def covered(path):
            return "." in allowed or path in allowed or any(p in allowed for p in ancestors(path))

        fields = delta.decode("utf-8", "strict").split("\0")[:-1]
        changes = list(zip(fields[::2], fields[1::2]))
        # git-add of a new child also deletes an old blob at its parent path.
        replaced = {parent for status, path in changes if status == "A" and covered(path)
                    for parent in ancestors(path)}
        unexpected = [path for status, path in changes
                      if not covered(path) and not (status == "D" and path in replaced)]
        if unexpected:
            raise GitViewspaceError(
                "INCOMPLETE_SYNC_MANIFEST", "staged changes exceed the sync manifest",
                detail=", ".join(unexpected[:20]),
            )

    def _stage_compact(self, repository, base_commit, manifest, roots, reset_index):
        index = Path(repository).resolve() / ".git" / "index"
        lock = index.with_name("index.lock")
        try:
            handle = lock.open("xb")
        except FileExistsError as error:
            raise GitViewspaceError("GIT_FAILED", "Git index is locked", detail=str(lock)) from error
        environment = {"GIT_INDEX_FILE": str(lock)}
        nested_lock = lock.with_name("index.lock.lock")
        installed = False
        nested_available = not nested_lock.exists()

        def run(arguments, **kwargs):
            return self._repo_with_environment(
                repository, arguments, environment_overrides=environment, **kwargs,
            )

        try:
            with handle, index.open("rb") as source:
                shutil.copyfileobj(source, handle)
            if not nested_available:
                raise GitViewspaceError("GIT_FAILED", "temporary Git index is locked", detail=str(nested_lock))
            if reset_index:
                run(("reset", "--mixed", base_commit))
            run(("--literal-pathspecs", "add", "-A", "--force",
                 "--pathspec-from-file=-", "--pathspec-file-nul"),
                input_bytes=b"".join(path.encode("utf-8") + b"\0" for path in roots))
            delta = run(("diff-index", "--cached", "--name-status", "-z", "--no-renames",
                         "--no-ext-diff", "--no-textconv", base_commit, "--")).stdout
            self._validate_staging_delta(delta, manifest)
            tree = self._oid_from_result(run(("write-tree",)), "index tree identity")
            os.replace(lock, index)
            installed = True
            return tree
        finally:
            if not installed:
                if nested_available:
                    nested_lock.unlink(missing_ok=True)
                lock.unlink(missing_ok=True)

    def stage_manifest(self, repository, base_commit, manifest, *, reset_index=True):
        roots = self._staging_roots(manifest)
        if roots is not manifest:
            return self._stage_compact(repository, base_commit, manifest, roots, reset_index)
        # Only an uninterrupted compound advance may reuse prepare_sync_checkout's proof.
        if reset_index:
            self.run(
                (
                    "-c",
                    "core.hooksPath=/dev/null",
                    "-C",
                    repository,
                    "reset",
                    "--mixed",
                    base_commit,
                )
            )
        if manifest:
            payload = b"".join(path.encode("utf-8") + b"\0" for path in manifest)
            self.run(
                (
                    "-c",
                    "core.hooksPath=/dev/null",
                    "-C",
                    repository,
                    "--literal-pathspecs",
                    "add",
                    "-A",
                    "--force",
                    "--pathspec-from-file=-",
                    "--pathspec-file-nul",
                ),
                input_bytes=payload,
            )
        return self.index_tree(repository)

    def commit_index(
        self,
        repository,
        branch,
        base_commit,
        base_tree,
        tree,
        message,
        identity,
        *,
        verify_index=True,
    ):
        if not isinstance(message, str) or not message or "\0" in message:
            raise GitViewspaceError("INVALID_MESSAGE", "commit message must be nonempty and contain no NUL")
        head, checkout_branch = self.checkout_position(repository)
        if head != base_commit or checkout_branch != branch:
            raise GitViewspaceError("CHECKOUT_DIVERGED", "checkout changed before commit")
        if verify_index and self.index_tree(repository) != tree:
            raise GitViewspaceError("INDEX_DIVERGED", "Git index changed before commit")
        if tree == base_tree:
            return base_commit, False
        self.run(
            (
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "commit.gpgSign=false",
                "-C",
                repository,
                "commit",
                "--no-gpg-sign",
                "--no-verify",
                "-m",
                message,
            ),
            identity=identity,
        )
        commit, checkout_branch = self.checkout_position(repository)
        if checkout_branch != branch:
            raise GitViewspaceError("COMMIT_FAILED", "commit did not advance the owned branch")
        line = self._repo(repository, ("rev-list", "--parents", "-n", "1", commit)).stdout
        try:
            identities = line.decode("ascii", "strict").split()
        except UnicodeDecodeError as error:
            raise GitViewspaceError("COMMIT_FAILED", "Git returned invalid commit ancestry") from error
        if identities != [commit, base_commit] or self.tree_oid(repository, commit) != tree:
            raise GitViewspaceError("COMMIT_FAILED", "commit tree or parent differs from staged transition")
        return commit, True

    def _reject_transforming_attributes(self, repository):
        files = self._repo(repository, ("ls-files", "-z")).stdout
        if not files:
            return
        result = self._repo(
            repository,
            (
                "check-attr",
                "-z",
                "--stdin",
                "filter",
                "working-tree-encoding",
                "ident",
                "eol",
                "text",
                "merge",
            ),
            input_bytes=files,
        )
        fields = result.stdout.split(b"\0")
        if fields and fields[-1] == b"":
            fields.pop()
        if len(fields) % 3:
            raise GitViewspaceError("INVALID_REPOSITORY", "invalid Git attribute output")
        for index in range(0, len(fields), 3):
            path, attribute, value = fields[index : index + 3]
            if value not in (b"unspecified", b"unset"):
                label = path.decode("utf-8", "replace")
                name = attribute.decode("ascii", "replace")
                raise GitViewspaceError(
                    "UNSUPPORTED_GIT_ATTRIBUTES",
                    f"unsupported working-tree attribute {name} on {label}",
                )

    def validate_repository(
        self,
        repository,
        *,
        expected_branch=None,
        check_claim=True,
        owner_uid=None,
        require_clean=True,
    ):
        repository = Path(repository)
        try:
            metadata = repository.lstat()
        except FileNotFoundError as error:
            raise GitViewspaceError("REPOSITORY_NOT_FOUND", f"repository does not exist: {repository}") from error
        if repository.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
            raise GitViewspaceError("INVALID_REPOSITORY", "repository must be a real directory")
        repository = repository.resolve()

        if owner_uid is not None:
            for base, directories, files in os.walk(repository, followlinks=False):
                for name in [".", *directories, *files]:
                    path = Path(base) if name == "." else Path(base) / name
                    entry = path.lstat()
                    if not (
                        stat.S_ISDIR(entry.st_mode)
                        or stat.S_ISREG(entry.st_mode)
                        or stat.S_ISLNK(entry.st_mode)
                    ):
                        raise GitViewspaceError(
                            "UNSUPPORTED_REPOSITORY",
                            f"repository contains an unsupported filesystem entry: {path}",
                        )
                    if entry.st_uid != owner_uid:
                        raise GitViewspaceError(
                            "REPOSITORY_OWNER_MISMATCH",
                            f"repository entry is not owned by UID {owner_uid}: {path}",
                        )

        dot_git = repository / ".git"
        try:
            git_metadata = dot_git.lstat()
        except FileNotFoundError as error:
            raise GitViewspaceError("INVALID_REPOSITORY", "repository must have a .git directory") from error
        if dot_git.is_symlink() or not stat.S_ISDIR(git_metadata.st_mode):
            raise GitViewspaceError("INVALID_REPOSITORY", "linked and gitfile worktrees are unsupported")

        inside = self._repo(repository, ("rev-parse", "--is-inside-work-tree")).stdout.strip()
        bare = self._repo(repository, ("rev-parse", "--is-bare-repository")).stdout.strip()
        top = Path(
            self._repo(repository, ("rev-parse", "--path-format=absolute", "--show-toplevel"))
            .stdout.decode("utf-8")
            .strip()
        ).resolve()
        git_directory = Path(
            self._repo(repository, ("rev-parse", "--path-format=absolute", "--absolute-git-dir"))
            .stdout.decode("utf-8")
            .strip()
        ).resolve()
        common_directory = Path(
            self._repo(repository, ("rev-parse", "--path-format=absolute", "--git-common-dir"))
            .stdout.decode("utf-8")
            .strip()
        ).resolve()
        if inside != b"true" or bare != b"false" or top != repository:
            raise GitViewspaceError("INVALID_REPOSITORY", "repository is not an ordinary checkout")
        if git_directory != dot_git.resolve() or common_directory != git_directory:
            raise GitViewspaceError("INVALID_REPOSITORY", "linked worktrees are unsupported")

        object_format = self._repo(repository, ("rev-parse", "--show-object-format")).stdout.strip()
        if object_format != b"sha1":
            raise GitViewspaceError("UNSUPPORTED_OBJECT_FORMAT", "only SHA-1 repositories are supported")
        shallow = self._repo(repository, ("rev-parse", "--is-shallow-repository")).stdout.strip()
        if shallow != b"false" or (git_directory / "shallow").exists():
            raise GitViewspaceError("UNSUPPORTED_REPOSITORY", "shallow repositories are unsupported")
        if (git_directory / "commondir").exists():
            raise GitViewspaceError("UNSUPPORTED_REPOSITORY", "linked worktrees are unsupported")
        alternates = git_directory / "objects/info/alternates"
        if alternates.exists() and alternates.stat().st_size:
            raise GitViewspaceError("UNSUPPORTED_REPOSITORY", "alternate object stores are unsupported")
        if self._repo(repository, ("replace", "-l")).stdout:
            raise GitViewspaceError("UNSUPPORTED_REPOSITORY", "replace objects are unsupported")
        promisor = self._repo(
            repository,
            ("config", "--get-regexp", r"^remote\..*\.promisor$"),
            check=False,
        )
        if promisor.returncode == 0 and promisor.stdout.strip():
            raise GitViewspaceError("UNSUPPORTED_REPOSITORY", "partial clones are unsupported")
        if promisor.returncode not in (0, 1):
            raise GitViewspaceError("INVALID_REPOSITORY", "cannot inspect partial-clone configuration")

        if self._config_bool(repository, "core.sparseCheckout") is True:
            raise GitViewspaceError("UNSUPPORTED_REPOSITORY", "sparse checkouts are unsupported")
        autocrlf = self._config_bool(repository, "core.autocrlf")
        if autocrlf not in (None, False):
            raise GitViewspaceError("UNSUPPORTED_REPOSITORY", "core.autocrlf must be false")
        if self._config_bool(repository, "core.filemode") is False:
            raise GitViewspaceError("UNSUPPORTED_REPOSITORY", "core.filemode must be true")
        merge_drivers = self._repo(
            repository,
            ("config", "--local", "--get-regexp", r"^merge\..*\.driver$"),
            check=False,
        )
        if merge_drivers.returncode == 0 and merge_drivers.stdout.strip():
            raise GitViewspaceError("UNSUPPORTED_REPOSITORY", "custom Git merge drivers are unsupported")
        if merge_drivers.returncode not in (0, 1):
            raise GitViewspaceError("INVALID_REPOSITORY", "cannot inspect custom Git merge drivers")

        unresolved = self._repo(repository, ("ls-files", "-u", "-z")).stdout
        if unresolved:
            raise GitViewspaceError("DIRTY_REPOSITORY", "repository has unresolved index entries")
        dirty = self._repo(
            repository,
            ("status", "--porcelain=v1", "-z", "--untracked-files=all"),
        ).stdout
        if dirty and require_clean:
            raise GitViewspaceError("DIRTY_REPOSITORY", "repository checkout is not clean")

        for marker in (
            "MERGE_HEAD",
            "CHERRY_PICK_HEAD",
            "REVERT_HEAD",
            "REBASE_HEAD",
            "BISECT_LOG",
            "rebase-apply",
            "rebase-merge",
            "sequencer",
        ):
            if (git_directory / marker).exists():
                raise GitViewspaceError("GIT_OPERATION_IN_PROGRESS", f"Git operation is in progress: {marker}")

        head = self._repo(repository, ("rev-parse", "--verify", "HEAD^{commit}"))
        head_text = head.stdout.decode("ascii").strip()
        if len(head_text) != 40 or any(character not in "0123456789abcdef" for character in head_text):
            raise GitViewspaceError("INVALID_REPOSITORY", "repository HEAD is not a SHA-1 commit")

        tree = self._repo(repository, ("ls-tree", "-r", "-z", "HEAD")).stdout
        for record in tree.split(b"\0"):
            if not record:
                continue
            try:
                metadata, encoded_path = record.split(b"\t", 1)
                mode, object_type, object_id = metadata.split(b" ", 2)
                path = encoded_path.decode("utf-8")
            except (ValueError, UnicodeDecodeError) as error:
                raise GitViewspaceError(
                    "UNSUPPORTED_REPOSITORY",
                    "Git tree contains an invalid or non-UTF-8 entry",
                ) from error
            if any(component == ".git" for component in path.split("/")):
                raise GitViewspaceError("UNSUPPORTED_REPOSITORY", "Git tree contains a .git component")
            if mode == b"160000":
                if object_type != b"commit":
                    raise GitViewspaceError("UNSUPPORTED_REPOSITORY", "invalid gitlink type")
                continue
            if mode not in {b"100644", b"100755", b"120000"} or object_type != b"blob":
                raise GitViewspaceError("UNSUPPORTED_REPOSITORY", f"unsupported Git tree mode at {path}")
            if mode == b"120000":
                target = self._repo(
                    repository,
                    ("cat-file", "blob", object_id.decode("ascii")),
                ).stdout
                try:
                    target.decode("utf-8")
                except UnicodeDecodeError as error:
                    raise GitViewspaceError(
                        "UNSUPPORTED_REPOSITORY",
                        f"symbolic-link target is not UTF-8: {path}",
                    ) from error
                if b"\0" in target:
                    raise GitViewspaceError(
                        "UNSUPPORTED_REPOSITORY",
                        f"symbolic-link target contains NUL: {path}",
                    )

        if b"160000 " in tree:
            from .submodules import entries
            entries(repository, head_text)
        self._reject_transforming_attributes(repository)
        fsck = self._repo(
            repository,
            ("fsck", "--connectivity-only", "--no-dangling"),
            check=False,
        )
        if fsck.returncode != 0:
            raise GitViewspaceError(
                "INVALID_REPOSITORY",
                "repository object connectivity check failed",
                detail=fsck.stderr.decode("utf-8", "replace").strip(),
            )

        symbolic = self._repo(repository, ("symbolic-ref", "-q", "HEAD"), check=False)
        if symbolic.returncode == 0:
            reference = symbolic.stdout.decode("utf-8").strip()
            branch = reference.removeprefix("refs/heads/")
        elif symbolic.returncode == 1:
            branch = None
        else:
            raise GitViewspaceError("INVALID_REPOSITORY", "cannot inspect repository HEAD")
        if expected_branch is not None and branch != expected_branch:
            raise GitViewspaceError(
                "CLONE_BRANCH_MISMATCH",
                f"clone did not check out branch {expected_branch}",
            )

        if check_claim:
            if (git_directory / "git-viewspace-owner.json").exists():
                raise GitViewspaceError("REPOSITORY_CLAIMED", "repository is already claimed by git-viewspace")
            if (git_directory / "git-viewspace.lock").exists():
                raise GitViewspaceError("REPOSITORY_CLAIMED", "repository has an unexplained git-viewspace lock")

        return RepositoryFacts(repository, git_directory, head_text, branch, not dirty)
