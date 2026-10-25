# Release status and qualification

Git Viewspace 0.1.0 is distributed as source at
[adapt-toolkit/git-viewspace](https://github.com/adapt-toolkit/git-viewspace).
Install from the repository root. No PyPI release is claimed.

## Supported scope

One installable distribution contains the manager, filesystem backend, and
synchronization helper. The two internal Python modules and two command entry
points remain separate. Apache-2.0 applies to this project; see LICENSE and
NOTICE for third-party attribution.

Supported hosts use Linux, FUSE, OverlayFS, and local ext4 writable layers.
There is no claim of security isolation, cross-platform support, SHA-256 Git
support, arbitrary submodule support, or compatibility with differently
versioned managed roots. Review the installation and security documentation.

## Local qualification

The 0.1.0 consolidation was qualified on Python 3.13 on 2026-10-16:

- Registered regression suite: 80 cases passed, no failures or skips.
- Package metadata checks: two cases passed.
- Wheel and source-distribution builds succeeded. The wheel includes both
  modules, console entry points, the synchronization helper, LICENSE and NOTICE.
- Fresh, non-editable installation passed metadata, import-location, and both
  command-help checks.
- Isolated filesystem source and installed-wheel tests passed.
- Actual FUSE/OverlayFS tests passed writable sync, stage, commit, remount,
  dirty-view recovery after mount loss, and daemon restart. Test mounts were
  cleaned up.

These are bounded acceptance results, not exhaustive crash testing or an
independent security audit. Test counts can overlap; do not add them as distinct
coverage. Hosted CI tests Python 3.11 and 3.13; check the repository Actions tab
for the results of a particular commit.

## Release maintenance

Before distributing a new version:

1. Run the registered tests and installed-artifact acceptance checklist.
2. Qualify privileged mount/recovery behavior on a disposable supported host.
3. Review the exact release tree and archives for secrets and accidental data.
4. Check dependency licenses when distributing bundled wheels or images:
   pygit2 has GPLv2 with a linking exception, fusepy has ISC, and native/system
   dependencies retain their own terms.
5. Publish versioned artifacts and checksums deliberately. A source commit does
   not imply a package-index release or a security certification.

## Optional routing qualification

The routing branch adds an optional separately built native helper; it does not
bundle libfuse or replace the two existing Python entry points. See
[routing installation and limitations](apps/git-viewspace/doc/routing.md).

The 2026-10-25 Linux/Python 3.13 qualification passed 118 registered cases with
zero failures, plus a clean 39-case basic run. Four interpreter-matrix cases were
skipped for missing prerequisites, not counted as successful coverage. Independent
review, real mounted failure/recovery tests, a fresh installed-package check and
random-I/O measurements are recorded under development/routing. This is not an
independent security audit or proof of every possible crash interleaving.

The Python 3.13 source-and-wheel matrix case subsequently passed after installing
its missing build dependencies. Python 3.11, 3.12 and 3.14 were unavailable on this
host and remain unqualified by this run.
