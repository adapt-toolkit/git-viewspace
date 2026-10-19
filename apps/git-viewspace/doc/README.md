# git-viewspace documentation

`git-viewspace` manages persistent read-only and writable filesystem views of Git
commits. One initialized root owns one ordinary Git checkout, one daemon, one
shared git-viewspace-fs process, and any number of named views.

The current implementation includes:

- all four root initialization modes;
- daemon start, status, recovery, supervision, and forceful stop;
- persistent read-only views;
- writable OverlayFS views;
- sync, forced sync-manifest staging, commit, clean, and switch operations;
- `view init-and-start` and `view advance` compositions; and
- human-readable and stable JSON command output.

The conservative implementation-hardening pass and Stage 8b packaging and
documentation pass are complete. Stage 8b added installed-artifact acceptance,
service-manager guidance, and consistent operator documentation without
introducing another core view lifecycle or new runtime semantics.

## Documents

- [CLI reference](cli.md): commands, arguments, prerequisites, and effects.
- [Daemon and recovery](daemon.md): process ownership, persistence, recovery,
  and shutdown.
- [Behavior and limitations](behavior.md): filesystem and Git semantics,
  including `.gitignore` and unconditional staging.
- [Examples](examples.md): complete read-only and writable workflows.
- [Installation and host prerequisites](installation.md): supported host,
  package installation, privilege boundary, and storage requirements.
- [Running the daemon as a service](service.md): foreground supervision,
  restart behavior, and orderly shutdown.
- [Operations and troubleshooting](operations.md): status, common failures,
  interrupted transitions, and cleanup verification.
- [Installed-artifact acceptance](acceptance.md): the bounded Stage 8b release
  checklist.
- [Pinned submodules](../../../SUBMODULES.md): explicit acquisition and pin updates.

The normative contracts remain in
[`../spec/git-viewspace-specification.md`](../spec/git-viewspace-specification.md) and
[`../spec/git-viewspace-architecture.md`](../spec/git-viewspace-architecture.md). These
documents explain the implemented program; the specifications decide details
when wording differs.

## Minimal setup

The MVP runs on Linux and requires Git, FUSE, OverlayFS, local ext4 storage for
writable upper/work directories, Python 3.11 or newer, and permission to run
the manager's fixed mount, unmount, and OverlaySync helper commands through
`sudo`.

Install the single root Python package into a virtual environment. See
[Installation and host prerequisites](installation.md) before configuring an
unattended daemon:

```sh
python3 -m pip install .
```

Then initialize and start one root:

```sh
ROOT="$HOME/.local/state/git-viewspace/project"

git-viewspace --root "$ROOT" init \
  --git-name "VIEWSPACE User" \
  --git-email user@example.invalid \
  --clone https://example.invalid/project.git

git-viewspace --root "$ROOT" start
```

Initialization and normal CLI use must run as the same non-root user. The
manager invokes privilege only at its fixed mount and replay boundaries.
