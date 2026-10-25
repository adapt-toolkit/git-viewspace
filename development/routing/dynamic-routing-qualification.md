# Dynamic routing qualification

## Result

The final exhaustive/full-profile run passed 119 registered cases, skipped four
interpreter source/wheel matrix cases for missing dependencies, and had no failures
or infrastructure errors. The focused final routing run passed all three selected
cases. Independent review found no remaining routing correctness blocker.

Earlier runs caught stale explicit test registrations and rejected test definitions
changed during a run. Registrations were corrected and the complete final run used
frozen definitions. These earlier runs are not counted as final qualification.

## Exercised behavior

- Native descriptor format validation, bounded reads and UTF-8/error cases.
- Lazy startup context creation without reading or walking the view.
- Atomic descriptor creation/replacement, invalid replacement, open-source and
  hardlink rejection, virtual deletion/reactivation, and old open target handles.
- Paginated enumeration of 700 descriptors with unavailable external backing,
  preserving descriptor visibility without opening backing files during listing.
- External replacement faults across restart, explicit rebind, fault-persistence
  failure recovery and legacy admission/fault conversion.
- Runtime sockets, readonly bindings, protected ancestors, directory fsync and
  exclusion of external bytes/runtime objects from commits.
- Narrow subtree checkpointing preserves unrelated tracked and pre-existing
  uncommitted outside data; runtime marker prefixes and separate ordinary views work.
- Four lower-daemon restart cycles plus three same-daemon remounts per cycle passed
  the focused origin-recovery fixture; the registered origin case also passed.
- Existing contention, submodule and publication regression cases passed.

## Performance and limits

See [performance.md](performance.md) and the reproducible startup benchmark.
Startup measured roughly 0.26–0.34 seconds for both 100-file and 100,000-file routed
fixtures. Routed metadata overhead remains substantial; --subtree confines it to
session data while large datasets can use ordinary views. No claim of near-native
metadata performance is made. Ordinary data callback counters remained zero,
confirming kernel passthrough.

A new lower-FUSE incarnation still requires upper-layer origin recovery; same-lower
remounts skip it. The optional helper now uses protocol version 2 and needs OpenSSL
libcrypto development support in addition to libfuse. Rebuild the helper when
upgrading this branch. Agent-operator integration is explicitly deferred.

Raw local harness reports and disposable test logs are kept outside committed
source. The tests assert cleanup of their owned mounts; no production root or
inference provider was used.
