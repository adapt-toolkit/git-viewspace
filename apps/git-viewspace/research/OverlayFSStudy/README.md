# Archived OverlayFS Study

This directory is a non-executable archive of the completed GitViewspaceFS and
OverlayFS qualification study. The historical Python runner, privileged
capture machinery, scenario implementation, and 45-test harness were removed
after the study completed; this directory is not a runnable package or a
maintained product test suite.

The preserved record consists of:

- [`RESULT.md`](RESULT.md), the human-readable conclusion and qualification
  summary;
- [`docs/study-specification.md`](docs/study-specification.md), the completed
  scope, procedure, evidence contract, and exit criteria; and
- the path-neutral
  [qualification capsule](docs/README.md), including the exact scenario
  registry and artifact grammar, complete scenario results, environment and
  fixture contracts, and hashes that bind this compact archive to the omitted
  canonical evidence bundle.

The canonical run was `956a13f5-26e8-4ebb-8925-5783594d79aa`: all 35 study
scenarios passed under evidence format `viewspace-stage0-evidence-v2`. The capsule
does not claim to contain the 3,937-file raw evidence bundle; its manifest
records that bundle's digest and the limits of the retained evidence.

The normative production contract derived from this research is the
[Qualified OverlayFS Upper Artifact Format](../../spec/OverlayFS-artifact-format.md).
If the qualified kernel, filesystem, FUSE, or OverlayFS profile changes, create
a new separately qualified experiment rather than treating this frozen archive
as an executable regression suite.
