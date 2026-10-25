# Development

The manager lives in `apps/git-viewspace/git_viewspace`; the FUSE backend in
`apps/git-viewspace-fs/git_viewspace_fs`. Tests and a standalone harness are
included. No external engine or application checkout is needed.

Install the root package (`pip install .`) and `requirements-test.txt` into a virtual environment.
Run the harness from the repository root with that environment on PATH:

```sh
export PATH="$PWD/.venv/bin:$PATH"
export VIEWSPACE_WORKSTREAM_SCRATCH="$(mktemp -d /tmp/git-viewspace-tests.XXXXXX)"
python3 utils/viewspace_test.py run --scope basic \
  --scratch-root "$VIEWSPACE_WORKSTREAM_SCRATCH/basic" \
  --report "$VIEWSPACE_WORKSTREAM_SCRATCH/basic.json" \
  --html-report "$VIEWSPACE_WORKSTREAM_SCRATCH/basic.html"
```

For mount-enabled acceptance, use a disposable Linux host with the documented
sudo/FUSE/ext4 facilities and run `--scope full --profile full`. Missing host
facilities are not a successful mount test. Never run host tests against a
valuable managed root. Inspect and stop test-owned daemons before deleting
their scratch directories.

See [test authoring](tests/how_to_write_tests.md) for descriptors and exact
test registration. Preserve upstream notices. Changes should include focused
regressions, especially for lifecycle, crash recovery, and concurrency behavior.
Contributions are under Apache-2.0; only contribute code you are authorized to license.

For optional routing acceptance, build the helper as described in
[routing](apps/git-viewspace/doc/routing.md), set `VIEWSPACE_ROUTER_BIN` to its
absolute path, and run `--scope exhaustive --profile full`. The `host` profile
alone excludes cases also labelled `privileged`. Routed host tests create their
own disposable roots and assert cleanup; never point them at a valuable root.
