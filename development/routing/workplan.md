# Routing workplan

1. **Experiments (completed initially):** isolated checkout; inspect architecture;
   test installed libfuse/kernel APIs; real forwarding mounts and full stack;
   concurrent handles/copy-up; first random-I/O comparison. Evidence: experiments.md.
2. **Specification review:** fresh read-only reviewer checks specification and this
   plan, particularly overengineering of rare cases that can be rejected. Resolve
   or explicitly explain declined recommendations; commit the reviewed plan.
3. **Router executable:** native ordinary forwarding/passthrough, native resource
   ownership, special binding map and pinned handles, fault reporting, readonly
   enforcement. Focused mounted tests and build/install support. No GPL example
   code imported; retain Apache-2.0 and library licensing notices.
4. **Manager integration:** optional per-view schema and CLI; private underlying
   mount, router process lifecycle, status/rebind, durable fault identity; existing
   advance and rollback; mounted lifecycle/commit tests.
5. **Performance qualification:** benchmark implementation using reproducible tools,
   alternating direct/routed order, warm-up and repeats; multiple block sizes/files,
   random reads/writes, concurrency, latency tails, open/metadata costs; inspect
   counters for unexpected userspace data reads. Fix material regressions without
   adding large machinery. Report any unresolved design/performance concern.
6. **Independent code review:** a fresh read-only reviewer examines actual code,
   tests and benchmark evidence for correctness, simplicity and hidden performance
   penalties. Evaluate recommendations; record decisions rather than blindly adopt.
7. **Regression qualification:** full applicable unchanged unit/harness tests and
   mount-enabled acceptance. Missing facilities are not passes. Inspect test-owned
   process/mount cleanup and resolve regressions.
8. **Publication:** consistent public docs/help/build instructions/security rationale,
   final requirement-by-requirement audit, clean branch commits, push branch and
   open pull request. Keep personal paths, private operational details, credentials,
   upstream GPL experiments and raw scratch traces out of the public repository.

Work remains active until the optional installed router, regression/benchmark
results, independent reviews, documentation and remote PR are all verified.
