# OverlayFS Study qualification capsule

This directory preserves the compact, durable subset of the canonical Stage 0
evidence needed to understand, reproduce, and implement against the qualified
OverlayFS artifact format. It supplements the normative
[Qualified OverlayFS Upper Artifact Format](../../../spec/OverlayFS-artifact-format.md).

The canonical run was
`956a13f5-26e8-4ebb-8925-5783594d79aa`. All 35 scenarios passed under evidence
format `viewspace-stage0-evidence-v2` and harness version 2.

## Contents

| File | Status | Purpose |
| --- | --- | --- |
| `study-specification.md` | Relocated completed specification | Qualification scope, procedure, evidence contract, and exit criteria. |
| `scenario-registry.json` | Exact copy | Ordered scenario definitions, operation arguments, capture points, aliases, and custom scenario names. |
| `artifact-format.json` | Exact copy | Machine-readable positive grammar, full retained occurrence maps, work-state inventory, compatibility binding, and validation results. |
| `qualification-profile.json` | Derived, path-neutral | Host, filesystem, FUSE/OverlayFS, workload, tool, source-file, and evidence bindings. |
| `fixture-contract.json` | Derived, lossless contract | Exact primary and secondary fixtures plus the deterministic 10,000-file large-tree generator. |
| `results-summary.json` | Derived, path-neutral | Outcome and duration for every scenario, without local workspace paths. |
| `capsule-manifest.json` | Derived index | Roles, derivation methods, hashes, and bindings for this capsule and the omitted full evidence bundle. |

The two exact-copy files retain their original evidence bytes and SHA-256
digests. Derived files deliberately remove host-local paths and bulky repeated
fixture entries. Their extraction rules and hashes are recorded by
`capsule-manifest.json`.

## Provenance limits

The run did not record Git HEAD and used a dirty working tree. No commit is
therefore claimed as its exact source. `qualification-profile.json` preserves
the authoritative per-file hashes from `runtime/run-fingerprint.json` and
identifies the first later Stage 0 result commit only as a subsequent snapshot.
Historical `stage0/` source labels remain in that map because the study was
relocated to `research/OverlayFSStudy/` after qualification.

The artifact grammar is empirical and bound to the recorded kernel,
GitViewspaceFS/FUSE lower, ext4 upper/work, identity, and OverlayFS option profile.
The study correlated stopped upper artifacts with independent merged-tree
oracles; it did not implement or prove the later `view sync` replay engine.

## Omitted raw evidence

The complete local evidence bundle is intentionally not committed. It contains
3,937 files totaling 63,913,381 bytes: one manifest plus 3,936
checksum-covered payload files. Its manifest SHA-256 is:

```text
ecd14695ea35ad76cc90331def5b22e96b4c4fd25851d07fd90555588b23aa21
```

The omitted expanded fixture manifest alone is 2,848,058 bytes. The compact
fixture contract records its exact source digest and deterministic expansion.
The raw bundle should be archived separately if byte-level checkpoint evidence
must survive beyond the qualifying host; this Git capsule is not represented
as a substitute for that archive.
