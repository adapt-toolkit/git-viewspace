# Test support

`viewspace_test.py` runs the registered test tree under `tests/`. It supports
basic, full, and exhaustive scopes; JSON and HTML reports; exact test selection;
process-tree supervision; and explicit golden updates.

Run `python3 utils/viewspace_test.py run --help` for options. Scratch storage must
be outside the source repository. Select an owned disposable directory and pass
explicit `--scratch-root`, `--report`, and `--html-report` paths.

The default Rust unit inventory is empty. Generic Cargo and registered-unit
adapters are retained, but no external application engine is built or invoked.
`viewspace_test_tree.py` handles bounded archives, no-follow materialization,
executable modes, symlinks, and deterministic filesystem comparisons.

See [test authoring](../tests/how_to_write_tests.md) and
[contributing](../CONTRIBUTING.md). Host tests require existing mount facilities;
do not change privilege policy implicitly to make tests pass.
