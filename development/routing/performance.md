# Router performance qualification (in progress)

Actual manager-mounted OverlayFS + routing FUSE, Linux 6.12, libfuse 3.17.
The reproducible benchmark is experiments/routing/benchmark_router.py. Results in
router-random-io-results.json use a warm 256 MiB disposable file, three rounds with
alternating direct/routed order, 4 KiB through 4 MiB random I/O and eight readers.
No global cache drops were used. These are measurements, not portable guarantees.

The first implementation unnecessarily left privilege-removal work to the upper
FUSE layer, producing large small-write overhead despite kernel data passthrough.
Negotiating HANDLE_KILLPRIV_V2 delegates that work to the native backing operations
and kernel passthrough. Actual writes/truncations through ordinary and external
bindings were verified to clear setuid/setgid bits, and writes to clear file
capabilities. Unsupported kernels retain the default FUSE behavior.

After this change, median routed/direct random-write ratios were 0.99, 0.98, 0.99,
1.03 at 4 KiB, 64 KiB, 1 MiB and 4 MiB respectively; read ratios were 0.97, 1.05,
1.09, 1.00. Ordinary read/write userspace callback counters stayed zero.

Open/stat/close still crosses userspace and has material overhead: this run measured
about 681 microseconds versus 8 microseconds directly. That is explicitly not a
near-native metadata claim. Further measurements below cover zero/many bindings, multifile operations and
readonly Git views. Preserve freshness and permission checks
rather than hiding this result through an unqualified attribute cache.

## Extended measurements

A separate direct-ext4/router fixture compared zero and 1,000 regular-file
bindings. Median open/stat/close was approximately 309 and 303 microseconds
respectively, versus 5 microseconds directly. Binding count did not measurably
increase that cost. Results are in router-binding-metadata-results.json.

Eight concurrent workers reading/writing eight disposable 256 MiB files completed
without errors; ordinary userspace data callbacks stayed zero. The first pass over
secondary files was not prewarmed, so the wall-time differences in
router-multifile-results.json are not claims of speedup. The benchmark supports
`--files 8` for this scenario; repeated alternating trials remain necessary on a
particular deployment.

For a separate immutable Git-backed 16 MiB file, three warm read trials measured
routed/direct median ratios of 1.14, 1.06, 0.77 and 1.04 at 4 KiB, 64 KiB, 1 MiB
and 4 MiB respectively (router-readonly-results.json). The below-one result shows
cache/scheduling variability, not an architectural speed advantage. These results
support kernel data passthrough while preserving the explicit metadata overhead.

## Dynamic routing and narrow-view fallback

The dynamic router no longer enumerates descriptor files at startup. A bounded
positive cache retains at most 256 bindings; open handles retain their own anchors.
The attempted absent-descriptor cache showed no material benefit and was removed.

`experiments/routing/benchmark_dynamic_startup.py` creates 100-file and 100,000-file
Git trees from a shared one-byte blob, without materializing the source checkout.
On this host, three routed starts measured 0.277/0.278/0.259 seconds (100 files)
and 0.315/0.335/0.306 seconds (100,000 files). Ordinary starts measured
0.296/0.262/0.214 and 0.416/0.246/0.225 seconds respectively. This establishes that
startup no longer performs a full namespace walk in this fixture, not a universal
latency guarantee. Metadata-heavy routed access remains substantially slower:
warm open/stat/close medians were about 797/793 microseconds versus 69/93 through
the ordinary view. Runs under concurrent regression load were noisier.

Therefore the optional `--subtree` selection is provided as the authorized fallback:
route only the session namespace and access large datasets through an ordinary
view. This confines rather than conceals the metadata penalty. Full-tree checkpoint
preservation is tested, including pre-existing changes outside the visible subtree.

Origin reset is now skipped on same-lower-incarnation remounts using a durable epoch.
A new lower incarnation still traverses the changed upper layer for correctness;
it does not traverse the whole repository. A future lower-FUSE change may eliminate
that recovery traversal, but no such guarantee is claimed by this workstream.
