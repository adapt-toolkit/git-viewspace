# Git Viewspace

A way for multiple AI agents to cheaply collaborate on the same Git repository
without creating multiple clones.

Git Viewspace provides independent filesystem views backed by a shared Git
repository, with private writable layers and explicit commits and merges.
It is a collaboration tool, not a security sandbox.

## Why use it?

Give each agent its own working directory and branch while sharing one Git
object store. Read-only files come from immutable Git snapshots through FUSE;
writable views use Linux OverlayFS and store changed files in a private upper
layer. Creating a view does not copy the whole repository. The daemon
serializes publication operations so agents do not race on a shared Git index.
Conflicting edits still require resolution: filesystem separation does not
make incompatible changes merge automatically.

Result: one view takes a fraction of a second to create, the amount of disk space 
taken up by a view is roughly proportional to the amount of uncommitted data. You 
can have hundreds of views for a tiny fraction of the cost of cloning the repo every 
time. 

## Requirements and installation

Version 0.1.0 is one installable distribution. It contains separate manager
(`git_viewspace`) and filesystem (`git_viewspace_fs`) modules and both command
entry points. `git-viewspace-fs` is an internal helper, not a separate package.

Linux, Python 3.11+, Git, FUSE 2 compatibility libraries, `/dev/fuse`, OverlayFS,
and local ext4 storage for writable layers are required. Run as a non-root
user with non-interactive sudo permission for the documented mount and helper
operations. No privilege policy is installed automatically.

Clone the repository and install from its root:

```sh
git clone https://github.com/adapt-toolkit/git-viewspace.git
cd git-viewspace
python3 -m venv .venv
.venv/bin/python -m pip install .
export PATH="$PWD/.venv/bin:$PATH"
git-viewspace --help
```

Read [installation and privilege boundaries](apps/git-viewspace/doc/installation.md)
before starting the daemon. This is not a portable replacement for Git
worktrees: use ordinary worktrees when Linux mounts or a shared daemon are
undesirable.

## Two agents, one repository

Choose a short absolute root path; the control socket has a Linux path-length
limit. Replace the repository URL and author identity below.

```sh
ROOT="$HOME/viewspace/project"
git-viewspace --root "$ROOT" init --clone https://example.org/team/project.git \
  --git-name "Agent Coordinator" --git-email agent@example.org
git-viewspace --root "$ROOT" start
git-viewspace --root "$ROOT" view init-and-start overlay agent-a \
  --from-branch main --new-branch agent-a
git-viewspace --root "$ROOT" view init-and-start overlay agent-b \
  --from-branch main --new-branch agent-b
```

Give the agents `$ROOT/views/agent-a/mnt` and `$ROOT/views/agent-b/mnt`.
They edit files there, not in the manager's private checkout. To publish an
agent's changes and move its view forward:

```sh
git-viewspace --root "$ROOT" view advance agent-a --message "Implement feature A"
git-viewspace --root "$ROOT" status
```

`advance` stops the view, synchronizes, stages, commits, and restarts it.
Pause processes using that view before advancing. Staging follows the exact
sync manifest, including ignored files: keep build products outside views.
Merge the returned exact commit into the integration branch deliberately:

```sh
git-viewspace --root "$ROOT" branch merge agent-a --into main \
  --ensure-attempt-tip EXACT_COMMIT_FROM_ADVANCE --message "Integrate feature A" \
  --checked-out-destination detach-if-clean
```

The destination must not be owned by a writable view. Other existing views
retain their original snapshots until explicitly switched or recreated.
See [examples](apps/git-viewspace/doc/examples.md) for read-only views,
cleanup, and individual publication commands.

## Operations and limits

- One daemon owns each managed root. Use `start --foreground` under a supervisor.
- `--json` produces machine-readable CLI results. `status` exposes queued work,
  current operation, and the age of its last consistent observation.
- A timed-out mutation may still be running; inspect state before retrying.
- A shared decoded-content cache defaults to 256 MiB. Set
  `GIT_VIEWSPACE_FS_CONTENT_CACHE_BYTES` before daemon startup (`0` disables it).
  This bounds retained content, not total process memory.
- Submodules require explicit preparation. No network fetch occurs on file reads.
- Views are not isolation against malicious agents. They share a host identity;
  symlinks can point outside a view, and writable views are not containers.
- Existing roots created under earlier names are not automatically migrated.
  This release introduces renamed commands, modules, and internal identifiers.

## Documentation

- [Command reference](apps/git-viewspace/doc/cli.md)
- [Daemon and recovery](apps/git-viewspace/doc/daemon.md)
- [Behavior and limitations](apps/git-viewspace/doc/behavior.md)
- [Service supervision](apps/git-viewspace/doc/service.md)
- [Submodule support](SUBMODULES.md)
- [Troubleshooting](apps/git-viewspace/doc/operations.md)
- [Development and testing](CONTRIBUTING.md)
- [Release status and qualification](PUBLICATION.md)

## License

Apache License 2.0. Copyright 2026 Adapt Framework Solutions.
See [LICENSE](LICENSE) and [NOTICE](NOTICE) for retained third-party attribution.
The filesystem backend derives from RepoFS; dependency licenses remain separate.

## Support

To support Adapt Framework Solutions, visit the
[donation page](https://github.com/adapt-toolkit/ours-donate).

## Additional Information

For a way to connect your agents into a team, check out our other work at [Ours.network](https://ours.network) 
and [Agent Fleet](https://app.ours.network)

Shout out to our partners at [8Hats](https://8hats.ai) who built [Agent's Co-working](https://agentscoworking.com/)
