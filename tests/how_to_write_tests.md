# How to write VIEWSPACE tests

`utils/viewspace_test.py` is the only long-term test framework and public entry point for
harness-managed tests. Direct Cargo or other component-local test commands may be used temporarily
during development, but every retained test must be registered with and runnable through
`utils/viewspace_test.py`. A case owns its definition, fixture, and golden beneath one directory:

```text
tests/<component>/<topic>/<case>=<driver>+<outcome>/
  spec.json
  files/       # only when the case has an input fixture
  golden/
```

Segments match `[a-z][a-z0-9_-]*`. Outcomes are `sr` for controlled success and `fr` for a
controlled assertion or process failure. A timeout, signal, launch failure, malformed protocol,
missing prerequisite implementation, or harness fault is infrastructure failure and never
satisfies `fr`.

Omit `files/` when the case has no input fixture. A present `files/` must contain real fixture
content; an empty `files/` directory and `.gitkeep` placeholders are rejected as redundant
representations of an empty fixture. Empty child directories remain meaningful fixture entries.

For a high-cardinality fixture, make `files/source.zip` the only entry in `files/`. The harness
materializes its logical root directly; the archive name does not appear in the case workspace.
Source and golden archives preserve path bytes, empty directories, regular-file executability,
symbolic-link target bytes, and file bytes. Expanded trees remain the convenient authoring and
diagnostic form, but high-cardinality committed fixtures and results use deterministic ZIPs.

## Common manifest

Every new `spec.json` uses schema 2 and declares its regression `scope`:

```json
{
  "schema_version": 2,
  "scope": "full",
  "driver": "exec",
  "selector": "python::example",
  "labels": [],
  "prerequisites": ["command:python3"],
  "config": {}
}
```

The manifest driver must equal the case-directory driver. Unknown, duplicate, or missing required
fields are errors. `scope` is optional only for backward compatibility: an omitted scope runs in
every scope so an older authoring procedure cannot silently create untested behavior. New and
materially changed cases must declare it, and every report summary counts any unscoped compatibility
cases. `selector` is the registered selector for `urun`/`udir`, the exact libtest name for `cargo`,
and a stable diagnostic name for `exec`.

## Regression scopes

`scope` is the smallest cumulative regression set that owns a case:

| Descriptor value | Included by | Intended use |
|---|---|---|
| `basic` | `basic`, `full`, `exhaustive` | Frequent, deterministic regression for staged development. |
| `full` | `full`, `exhaustive` | Ordinary complete product, integration, host, packaging, recovery, and quality coverage at workstream completion. |
| `exhaustive` | `exhaustive` | Exotic, unusually defensive, performance, compatibility-tail, or credentialed coverage reserved for later scheduled regression. |
| omitted | every scope | Transitional fail-safe for cases written by an older procedure; never the intended state for a new case. |

Choose scope from behavior and execution cost together:

- Use `basic` for critical paths and low-cost ordinary contracts that should catch mistakes between
  stages. A Basic case must be deterministic and usable on the ordinary agent host; do not attach
  `host`, `privileged`, `package`, `performance`, or `slow` labels. Any Basic prerequisite skip
  fails the suite rather than producing a successful partial gate.
- Use `full` for ordinary retained coverage that is slower, redundant for localization, needs a
  provisioned host, exercises packaging, or completes a matrix not needed after every stage.
- Use `exhaustive` for exotic encodings or metadata, unusual corruption/concurrency states,
  high-cardinality or performance workloads, broad compatibility tails, and live external calls.

Scope describes scheduling importance. Labels describe facilities and prerequisites describe
availability; neither substitutes for scope. `full` and `exhaustive` are cumulative, so never copy
a Basic case into a second directory merely to make it run in Full. Changing an existing case's
scope changes the development gate and must be justified in the stage or workstream outcome report;
timing alone does not demote critical coverage.

Labels select profiles without changing a case definition. The fixed labels are `host`,
`privileged`, `slow`, `package`, and `performance`. The default profile includes unlabeled cases;
`host` also includes host-labelled cases; `full` includes every label.

Prerequisites are checked before execution and produce a visible skip when unavailable:

- `linux`, `root`, `scratch-ext4`, or `unix-seqpacket`;
- `command:<name>`;
- `env:<name>`;
- `python-module:<import-name>`, using the harness interpreter; or
- `python-module:<command>:<import-name>`, using that exact interpreter command.

Python-module prerequisites launch the named interpreter in isolated mode and require the module
to import successfully. Mere discoverability is insufficient because a native extension can be
installed but unloadable.

Do not label an ordinary deterministic test merely because it uses the host filesystem. Labels are
for tests that need a particular host facility or are deliberately excluded from the fast default.

## Registered Rust drivers

`urun` calls a registered no-context Rust function. Its config and fixture are empty:

```json
{
  "schema_version": 2,
  "scope": "basic",
  "driver": "urun",
  "selector": "component::registered_case",
  "labels": [],
  "prerequisites": [],
  "config": {}
}
```

Its golden is exact UTF-8 `golden/stdout`, including the final `VIEWSPACE-TEST-RESULT` line.

`udir` installs the fixture from `files/` when present, changes to its declared fixture-relative
directory, and passes exact named arguments to a registered `UnitDirectoryContext` case:

```json
"config": {
  "cwd": "workspace/subdirectory",
  "args": {"entity_name": "alpha"}
}
```

Its canonical committed golden is one deterministic `golden/expected.zip`, containing `stdout`
and the complete final `files/` tree. For authoring only, the equivalent expanded `golden/stdout`
plus `golden/files/` shape is accepted; golden update normalizes it to the archive.

## Direct executable driver

`exec` launches an argv vector directly—never through a shell—and gives each case a fresh temporary
workspace, populated from `files/` when the case has an input fixture:

```json
"config": {
  "argv": ["python3", "tool.py", "literal;not-shell"],
  "cwd": {"anchor": "workspace", "path": "."},
  "environment": {
    "inherit": ["HOME", "PATH"],
    "set": {"MODE": "test"}
  },
  "timeout_seconds": 10,
  "compare": ["status", "stdout", "stderr", "filesystem"],
  "filesystem": "result"
}
```

`cwd.anchor` is `workspace` or `repository`; its path is relative and cannot escape. Environment is
exactly the inherited names plus assigned values—undeclared variables are absent. `{workspace}` and
`{repository}` are replaced literally in argv elements and assigned environment values; no other
interpretation occurs.

`compare` chooses any nonempty set of `status`, `stdout`, `stderr`, and `filesystem`.
`filesystem` is null unless selected, then names a workspace-relative result tree. Corresponding
goldens are `status` (one decimal integer plus LF), UTF-8 `stdout`/`stderr`, and deterministic
`expected.zip`; expanded `golden/files/` is accepted for authoring. Output remains visible in HTML
even when it is not compared.

For a result containing intentional nondeterminism, select `filesystem-script` instead of
`filesystem` and add a regular `compare.py` at the case root. After the case process and all its
descendants quiesce, the harness runs:

```text
compare.py /absolute/case/golden/expected.zip /absolute/case-workspace/result
```

Exit `0` means match and exit `1` means a diagnosed mismatch; any other exit, signal, timeout,
invalid UTF-8 output, or retained descendant is infrastructure failure. Comparator stdout and
stderr remain visible for passing and failing cases. The script must compare authoritative values,
ordering, formats, uniqueness, and cross-artifact references—it must not obtain equality by merely
deleting volatile fields. It must not modify either argument. Golden update skips the old script
and records the complete actual result archive.

### Python unittest registration

Retained Python `unittest` methods in the harness, git-viewspace-fs, git-viewspace, and unprivileged
OverlaySync source trees are closed inventory. Each callable method whose name starts with `test`
on a concrete top-level `TestCase` must appear by its exact dotted ID in exactly one recognized
harness descriptor. Broad module,
class, or `unittest discover` selectors are forbidden because they hide scheduling, duplicate
registration, and newly added methods.

Group exact IDs only when they share one coherent subject, setup boundary, prerequisites, and scope.
A conjunction in a group name is a review signal: split the group if the joined subjects differ on
any of those boundaries. The Basic inventory case runs
`utils/python_test_inventory.py`, which parses test sources without importing optional product
dependencies, follows supported `unittest.TestCase` imports, aliases, local inheritance, and mixins
across nested owned modules, and rejects missing, duplicate, stale, broad, dynamic, and spoofed
registrations.
Adding or renaming an owned method therefore requires updating its focused descriptor in the same
change.

Keep collection statically explicit. The inventory fails closed on wildcard imports, unresolved or
imported class bases, conditional class definitions, `load_tests`, `runTest`, dynamic method/class
construction, and decorated or metaclass-driven TestCase ancestry. If one of those mechanisms is
genuinely required, extend the inventory and its adversarial contract tests in the same change; do
not bypass the harness account.

Ordinary suites use `python3 -m unittest` followed only by exact IDs. The isolated git-viewspace-fs
groups use `utils/run_git_viewspace_fs_unittests.py`; its `--fixture` option is present only for groups
that require the generated Git repository. The runner independently checks that every argument is
an exact discovered git-viewspace-fs method ID and rejects duplicates. Do not add another Python
discovery wrapper or list the same method in a secondary inventory table.

### Python pytest registration

Retained pytest functions under `viewspace-intake/tests` are also a closed inventory. Register every
top-level test function once by its exact node ID, for example
`viewspace-intake/tests/test_cli.py::test_extract_publishes_to_a_bundle_and_emits_no_record_stream`.
A parametrized function is one focused registration and owns all of its parameter leaves; do not
register individual generated parameter IDs. File, directory, class, and implicit-discovery
selectors are forbidden.

Group exact function IDs only when subject, setup boundary, prerequisites, and scope are identical.
The Basic `python-pytest-inventory` case statically discovers the owned functions and rejects
missing, duplicate, stale, broad, dynamic, imported, or spoofed registrations. It also rejects
skip and xfail constructs: a status-only focused case must not report success while silently
omitting one of its owned leaves. If collection needs inheritance, dynamic generation, imported
tests, or another unsupported shape, extend `utils/pytest_test_inventory.py` and its adversarial
contract tests in the same change.

The inventory locks the package's collection configuration and rejects `conftest.py` or a
shadowing `pytest.ini`, `.pytest.ini`, `pyproject.toml`, `tox.ini`, or `setup.cfg`. Extend the
inventory contract deliberately before introducing one of those mechanisms; collection settings
must never create or omit tests outside the descriptor account.

The standard argv is
`python3 -m pytest -q -p no:cacheprovider -p utils.pytest_no_skips` followed only by exact
function node IDs. The VIEWSPACE plugin turns every skipped, xfail, deselected, empty-parameter, or
otherwise nonexecuted pytest outcome into failure. Disable pytest's cache provider so a harness
run never writes temporary state into the source checkout. Set `PYTHONDONTWRITEBYTECODE=1`, point
`PYTHONPATH` at the tested source tree, set `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`, inherit only
`PATH`, and put `TMPDIR` in `{workspace}`. Run from the repository root. The inventory checks this
exact isolation and requires every owned pytest case to compare against expected status zero.

## Cargo driver

One Cargo case names one exact libtest function and target:

```json
{
  "schema_version": 2,
  "scope": "full",
  "driver": "cargo",
  "selector": "tests::round_trip",
  "labels": [],
  "prerequisites": [],
  "config": {
    "package": "viewspace-language",
    "target": {"kind": "lib", "name": "viewspace_language"},
    "timeout_seconds": 120
  }
}
```

Target kinds are `lib`, `bin`, and `test`. Cargo cases omit `files/` and use
`golden/status`—normally `0`. The harness performs one locked, all-feature workspace build, lists
each built libtest binary, proves the exact selector exists, and invokes one test per report line.
Every discovered non-ignored libtest must have exactly one case; missing and duplicate mappings are
inventory errors. An ignored or otherwise outside-harness test is not accepted coverage: register
it, remove it, or document why harness execution is impossible and obtain case-by-case approval for
an exception. The harness refuses an exact selector that executes zero tests. A Cargo failure is
controlled only when libtest returns its ordinary test-failure status; launch, signal, build,
discovery, and timeout failures are infrastructure errors.

## Running and filtering

The JSON report is the canonical machine-readable test record. The HTML report is its complete
human-readable presentation and does not independently redefine case outcomes.

Before running tests, agents set `VIEWSPACE_WORKSTREAM_SCRATCH` to the short absolute path of their
owned directory beneath the workspace collection's `temporary scratch directory/`; its `PURPOSE.md` records the
workstream and generated harness descendants. They also set `CARGO_TARGET_DIR` beneath it. Run
Basic regression between stages and before each stage commit:

```sh
export VIEWSPACE_WORKSTREAM_SCRATCH=/absolute/path/to/scratch/workstream
export CARGO_TARGET_DIR="$VIEWSPACE_WORKSTREAM_SCRATCH/cargo-target"
```

```sh
python3 utils/viewspace_test.py run --scope basic \
  --scratch-root "$VIEWSPACE_WORKSTREAM_SCRATCH/basic" \
  --report "$VIEWSPACE_WORKSTREAM_SCRATCH/basic.json" \
  --html-report "$VIEWSPACE_WORKSTREAM_SCRATCH/basic.html"
```

During implementation, exact case, segment, tag, and glob filters provide a faster inner loop. They
do not replace the Basic regression gate. At the completion of a workstream, run Complete
regression, including every environment label:

```sh
python3 utils/viewspace_test.py run --scope full --profile full \
  --scratch-root "$VIEWSPACE_WORKSTREAM_SCRATCH/full" \
  --report "$VIEWSPACE_WORKSTREAM_SCRATCH/full.json" \
  --html-report "$VIEWSPACE_WORKSTREAM_SCRATCH/full.html"
```

This selection includes both `basic` and `full` descriptors. Review its JSON summary and every
skip; required unavailable infrastructure is an explicit verification gap, not a pass. The
repeated word `full` is intentional: `--scope full` chooses regression coverage, while the older
`--profile full` admits every facility label. The `exhaustive` scope is defined now so
classification is stable, but it is not part of the current
agent workstream procedure. It will become the input to the later daily-regression design.

For backward compatibility, omitting `--scope` selects `exhaustive`; together with the default
unlabelled profile this preserves the historical command's selection. New process documents and
stage and specification implementation notes must always name the intended scope explicitly.

Selections combine by narrowing across option classes:

```sh
python3 utils/viewspace_test.py run --scope basic \
  --scratch-root "$VIEWSPACE_WORKSTREAM_SCRATCH/run" \
  --report "$VIEWSPACE_WORKSTREAM_SCRATCH/run.json" \
  --html-report "$VIEWSPACE_WORKSTREAM_SCRATCH/run.html" \
  --case viewspace-language::lib-viewspace-language::workflow-instantiation
python3 utils/viewspace_test.py run --scope basic \
  --scratch-root "$VIEWSPACE_WORKSTREAM_SCRATCH/run" \
  --report "$VIEWSPACE_WORKSTREAM_SCRATCH/run.json" \
  --html-report "$VIEWSPACE_WORKSTREAM_SCRATCH/run.html" --segment process
python3 utils/viewspace_test.py run --scope basic \
  --scratch-root "$VIEWSPACE_WORKSTREAM_SCRATCH/run" \
  --report "$VIEWSPACE_WORKSTREAM_SCRATCH/run.json" \
  --html-report "$VIEWSPACE_WORKSTREAM_SCRATCH/run.html" --tag exec
python3 utils/viewspace_test.py run --scope exhaustive \
  --scratch-root "$VIEWSPACE_WORKSTREAM_SCRATCH/run" \
  --report "$VIEWSPACE_WORKSTREAM_SCRATCH/run.json" \
  --html-report "$VIEWSPACE_WORKSTREAM_SCRATCH/run.html" --glob 'viewspace-filesystem/*/*timeout*'
python3 utils/viewspace_test.py run --scope full --profile full \
  --scratch-root "$VIEWSPACE_WORKSTREAM_SCRATCH/full" \
  --report "$VIEWSPACE_WORKSTREAM_SCRATCH/full.json" \
  --html-report "$VIEWSPACE_WORKSTREAM_SCRATCH/full.html"
python3 utils/viewspace_test.py run --scope exhaustive --profile full \
  --scratch-root "$VIEWSPACE_WORKSTREAM_SCRATCH/exhaustive" \
  --report "$VIEWSPACE_WORKSTREAM_SCRATCH/exhaustive.json" \
  --html-report "$VIEWSPACE_WORKSTREAM_SCRATCH/exhaustive.html"
python3 utils/viewspace_test.py run --scope basic \
  --scratch-root "$VIEWSPACE_WORKSTREAM_SCRATCH/basic" \
  --report "$VIEWSPACE_WORKSTREAM_SCRATCH/basic.json" \
  --html-report "$VIEWSPACE_WORKSTREAM_SCRATCH/basic.html"
```

Cases run in stable case-ID order. Unknown exact cases, invalid filters, and empty selections are
errors. Passing output is never collapsed in HTML; failures add exact stdout/stderr, status, or
filesystem diffs and infrastructure diagnostics.

## Creating goldens

Golden writes require an explicit filter or `--all`:

```sh
python3 utils/viewspace_test.py run --update-golden --scope exhaustive \
  --scratch-root "$VIEWSPACE_WORKSTREAM_SCRATCH/golden" \
  --report "$VIEWSPACE_WORKSTREAM_SCRATCH/golden.json" \
  --html-report "$VIEWSPACE_WORKSTREAM_SCRATCH/golden.html" --case component::topic::case
python3 utils/viewspace_test.py run --update-golden --all --scope full --profile full \
  --scratch-root "$VIEWSPACE_WORKSTREAM_SCRATCH/golden" \
  --report "$VIEWSPACE_WORKSTREAM_SCRATCH/golden.json" \
  --html-report "$VIEWSPACE_WORKSTREAM_SCRATCH/golden.html"
```

The harness replaces one case golden only after that case reaches its declared controlled outcome.
Skips, infrastructure failures, and unexpected outcomes preserve the old golden. Review every
updated archive or file, then rerun normally.

The harness's `$HOME/.scratch/viewspace-test` default exists only for backward compatibility and is not
accepted for agent work. Agents always pass `--scratch-root` beneath their short, workstream-owned
`temporary scratch directory/` directory. Real git-viewspace/FUSE cases declare `scratch-ext4`; they visibly skip rather
than attempting writable views on tmpfs or another unsupported filesystem.

Keep a custom scratch pathname short. VIEWSPACE-VM cases create managed roots below nested harness
workspaces, and `<managed-root>/run/manager.sock` may not exceed 103 encoded bytes. With the current
`r-`/`e-`/`v2-` nesting, the resolved absolute scratch root may be at most 47 encoded bytes. Check
the resolved explicit path before relying on it and choose a shorter workstream name when needed.
An overlong scratch root produces `SOCKET_PATH_TOO_LONG` before the selected test assertion runs.

## CI and external tests

Future CI invokes only `viewspace_test.py`; a command outside the harness is not CI coverage and is
removed by default. `tests/external_tests.md` is an exception inventory, not a second test runner.
Each entry must explain precisely why harness registration is not possible, identify what coverage
would be lost by removal, and record its case-by-case disposition; an unapproved entry is pending
removal or migration. Prefer `exec` plus labels/prerequisites over adding a driver, but do not hide
a shell pipeline, mutable shared fixture, privileged host orchestration, or multi-step workflow
inside an argv wrapper.

## Inspecting end-to-end repositories

The registered git-viewspace/FUSE and cross-layer cases are grouped under `tests/viewspace-engine/e2e/` and
`tests/viewspace-engine/critical/`. Repository goldens contain the complete captured repository
(including `.git`), `viewspace.log`, the command transcript, and—only at active checkpoints—the bounded
external runner state. The established continuous full-run result is:

```text
tests/viewspace-engine/e2e/autonomous-formula-preparation=exec+sr/golden/expected.zip
```

Unpack it directly into an otherwise empty workstream-owned scratch directory whose `PURPOSE.md`
identifies golden inspection, then inspect ordinary Git and VIEWSPACE state:

```sh
unzip tests/viewspace-engine/e2e/autonomous-formula-preparation=exec+sr/golden/expected.zip \
  -d "$VIEWSPACE_WORKSTREAM_SCRATCH"
git -C "$VIEWSPACE_WORKSTREAM_SCRATCH/repository" log --graph --decorate --all
git -C "$VIEWSPACE_WORKSTREAM_SCRATCH/repository" show main
find "$VIEWSPACE_WORKSTREAM_SCRATCH/repository/.viewspace/log" -type f -print | sort
find "$VIEWSPACE_WORKSTREAM_SCRATCH/repository" -name entity.json -print | sort
git -C "$VIEWSPACE_WORKSTREAM_SCRATCH/repository" ls-tree -r --name-only main \
  | grep '/work-record.json\|/formula-resolved.viewspace'
grep 'runner_step\|workflow_completion\|runner_termination\|branch_' \
  "$VIEWSPACE_WORKSTREAM_SCRATCH/viewspace.log"
```

Other archives cover active checkpoints, dynamic configuration reread, workflow rejection,
completion, and parent integration. Their `compare.py` wrappers bind volatile
names and hashes by validated occurrence and cross-artifact relationship while still comparing
event order, commit topology/messages/trees, file modes, symlinks, formulas, records, captures, and
retained runtime state.
