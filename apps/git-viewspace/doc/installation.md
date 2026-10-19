# Installation and host prerequisites

`git-viewspace` is an MVP for Linux hosts. It is not currently distributed through
a package index and does not install or configure system services, kernel
modules, or privilege policy.

## Supported host profile

The implemented profile requires:

- Linux with `/proc`, FUSE, and OverlayFS;
- Python 3.11 or newer;
- Git;
- `/dev/fuse` available to the host;
- local ext4 storage for writable OverlayFS upper and work directories; and
- non-interactive `sudo` authorization for the manager's fixed privileged
  operations.

The initializing caller must be a non-root user. That caller's effective UID
and primary GID become the root's manager and workload identity. Every later
`git-viewspace` command for that root must run as the same identity.

The complete path to `<root>/run/manager.sock` must be no more than 103 bytes.
`git-viewspace init` checks this before creating the root and reports the measured
path length if it is too long.

## Install into one environment

Install the single `git-viewspace` distribution into a virtual environment. The
manager locates `git-viewspace-fs` beside its own Python executable first and then
on `PATH`.

From a source checkout:

```sh
python3 -m venv "$HOME/.local/lib/git-viewspace"
"$HOME/.local/lib/git-viewspace/bin/python" -m pip install --upgrade pip
"$HOME/.local/lib/git-viewspace/bin/python" -m pip install .

"$HOME/.local/lib/git-viewspace/bin/git-viewspace" --help
"$HOME/.local/lib/git-viewspace/bin/git-viewspace-fs" --help
```

For a release artifact, install the `git_viewspace-0.1.0` wheel instead of
the source directory. An installation is complete only if `view
sync` can locate its installed OverlaySync helper without access to the source
checkout.

## Build release artifacts

Build the single distribution from the repository root, directing release
artifacts into a disposable directory:

```sh
python3 -m venv "$HOME/.scratch/git-viewspace-build"
"$HOME/.scratch/git-viewspace-build/bin/python" -m pip install --upgrade pip build

mkdir -p "$HOME/.scratch/git-viewspace-dist"
"$HOME/.scratch/git-viewspace-build/bin/python" -m build \
  --outdir "$HOME/.scratch/git-viewspace-dist" .
```

Inspect and install the generated archives from those directories. Do not use
an editable installation for release acceptance: editable mode can hide a
missing packaged helper by resolving files from the source checkout.

## Privileged-operation boundary

The daemon invokes `sudo -n`, so an interactive password prompt is never a
valid runtime path. Site policy must authorize the recorded ordinary user to
perform the following manager-controlled operation families:

- launch the fixed `git-viewspace-fs` executable to establish the private FUSE
  mount;
- create, remount read-only, and remove the manager's bind and OverlayFS
  mounts using the fixed `mount` and `umount` executables; and
- launch the installed OverlaySync helper through the fixed system Python for
  stopped-upper inspection and replay.

OverlaySync starts privileged so it can read kernel-owned OverlayFS metadata,
but it irreversibly changes to the root's recorded non-root UID/GID before it
writes the managed checkout. Ordinary repository mutation does not execute as
root.

The exact command arguments contain the managed root, generation, and mount
paths. Privilege policy is therefore deployment-specific; this repository
does not install a broad `/etc/sudoers` entry. Validate the policy
non-interactively before relying on unattended service startup.

## FUSE configuration

git-viewspace-fs uses `allow_other` and `default_permissions`. The host FUSE
configuration must permit `allow_other` (commonly through `user_allow_other`
in `/etc/fuse.conf`). The private git-viewspace-fs process is launched through the
manager's privileged mount boundary, while exposed entries carry the recorded
ordinary workload identity.

## Storage and path choices

Choose a short, absolute root path on local ext4, for example:

```sh
ROOT="$HOME/.local/state/git-viewspace/project"
```

OverlayFS option strings cannot represent manager paths containing comma or
colon, so writable view startup rejects such paths. Do not place a managed
root on network storage, FUSE storage, or a filesystem whose upper/work
semantics differ from the qualified ext4 profile.

The workload is expected to use only public paths below
`<root>/views/<view>/mnt`. It should not edit `<root>/repo`, configuration,
state, generations, or private mountpoints.

## Upgrade boundary

There is no in-place schema migration in the MVP. Preserve the managed root,
stop its daemon cleanly, and inspect release notes and schema versions before
installing a different manager build. Unknown on-disk schemas fail closed.
