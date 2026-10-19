# Installed-artifact acceptance checklist

This checklist qualifies a built Stage 8b artifact on one supported host. It
is intentionally bounded; the source-tree unit and mount-enabled suites remain
the detailed regression tests.

## Build and install

Use a disposable directory outside the repository:

1. build the unified git-viewspace wheel and source artifact;
2. inspect their contents for expected packages, console entry points, and
   documentation;
3. create a fresh virtual environment;
4. install only the built artifacts, without editable mode or `PYTHONPATH`;
5. verify `git-viewspace --help` and `git-viewspace-fs --help`; and
6. confirm the installed manager can locate its installed OverlaySync helper.

## Lifecycle

Using a short disposable ext4-backed managed root:

1. initialize an empty repository;
2. start the daemon and check healthy status;
3. create, start, inspect, stop, switch, and destroy a read-only view;
4. create and start a writable view;
5. make a small regular-file creation, modification, deletion, and directory
   rename through the public mount;
6. stop, sync, stage, commit, clean, switch, and restart the view;
7. confirm the new commit and remounted contents;
8. stop and restart the daemon to exercise desired-mount restoration;
9. deinitialize and destroy the view; and
10. stop the daemon and verify no mount, child, socket, or runtime residue.

The sync in step 6 must execute from the installed wheel. Passing only when
the source checkout happens to be the current directory is a packaging
failure.

## Evidence to retain

Record:

- artifact filenames and SHA-256 digests;
- Python, Git, kernel, FUSE, and OverlayFS versions used;
- the exact installed package versions;
- the managed root and scratch filesystem type;
- each command's exit status;
- final Git commit and tree IDs; and
- the final cleanup observation.

Do not retain credentials, clone URLs containing credentials, hostnames, or
the disposable managed root itself in the repository.

## Pass conditions

Stage 8b passes when:

- the unified distribution builds and installs without the source tree;
- all expected runtime components are present in the artifacts;
- the complete installed-package lifecycle succeeds;
- the committed tree matches the mounted changes;
- daemon restart restores desired views;
- shutdown leaves no manager-owned runtime residue; and
- the operator documentation describes the observed behavior and known
  recovery limits without overclaiming.

If the acceptance run exposes a runtime defect, stop the packaging pass and
review the code change separately. Stage 8b itself does not silently expand
runtime semantics.
