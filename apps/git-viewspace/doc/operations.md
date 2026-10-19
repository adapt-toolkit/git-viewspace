# Operations and troubleshooting

## Routine inspection

Use the CLI rather than inferring state from directory listings:

```sh
git-viewspace --root "$ROOT" status
git-viewspace --root "$ROOT" view status
git-viewspace --root "$ROOT" --json status
git-viewspace --root "$ROOT" --json view status work
```

Online status combines durable state with the daemon's observed processes and
mounts. Offline status is read-only and reports residue or inconsistency; it
does not silently repair it.

The private git-viewspace-fs diagnostic stream is retained at
`<root>/run/viewfs.stderr` while relevant. Runtime files are diagnostic rather
than authoritative and may disappear after a clean stop.

## Common startup failures

### Control socket path is too long

Choose a shorter managed root. The complete
`<root>/run/manager.sock` path is limited to 103 encoded bytes. Moving an
already initialized root is unsupported because its canonical path is bound
into durable ownership records; initialize a new root at the shorter path.

### `sudo` reports that a password is required

The manager deliberately uses `sudo -n`. Configure a narrow non-interactive
policy for the manager's mount and helper command families. Do not start the
daemon from an interactive shell merely to cache credentials; unattended
restart would still fail.

### git-viewspace-fs is not installed

Install `git-viewspace-fs` into the same virtual environment as `git-viewspace`, or make
its fixed executable available on the service `PATH`. Verify both console
commands before daemon startup.

### `allow_other` is rejected

Confirm that the host FUSE configuration permits `allow_other`, that
`/dev/fuse` is present, and that the kernel FUSE support is available.

### Overlay mount is rejected

Confirm that the managed root is on the qualified local ext4 storage, the
upper and work directories share a filesystem, the kernel supports OverlayFS,
and root paths contain neither comma nor colon.

## Busy views

`view stop` is orderly. If the public mount remains after an unmount failure,
the manager retries every five seconds for up to thirty seconds. Persistent
busy state still fails visibly: quiesce the workload, close open files and
current working directories beneath the mount, and retry. Do not manually
delete a mounted view directory.

Root-level `stop` is the forceful manager cleanup boundary and may use a lazy
unmount after orderly unmount fails. It still reports failure if a
manager-owned mount or child remains.

## Interrupted writable transitions

`view status` exposes durable `sync`, `stage`, and `next` phases. Retry the
matching primitive command or `view advance` only when the reported state is a
supported resume point.

If startup or status reports mismatched records, an unexplained generation,
or a foreign mount:

1. stop workload access;
2. preserve the complete managed root for diagnosis;
3. do not delete mounts, generations, transaction records, or Git refs by
   hand; and
4. recreate the managed root from its Git repository when no documented
   repair applies.

The MVP intentionally has no general journal capable of repairing every
process- or host-failure publication window.

## Retired private aliases

git-viewspace-fs pins an alias after resolving it. Removing that alias from durable
configuration prevents new resolution but does not evict it from the running
git-viewspace-fs process. An orderly daemon stop/start clears retired private
aliases. This is normally a small metadata cost; there is no automatic
eviction threshold in the MVP.

## Cleanup verification

After a successful root stop:

- `git-viewspace status` reports the daemon and git-viewspace-fs stopped;
- no public view path is mounted;
- `<root>/git-mnt` is not mounted;
- no manager-owned git-viewspace-fs process remains; and
- `run/manager.sock` and `run/runtime.json` are absent.

Do not report or automate successful cleanup solely from the daemon process
having exited; mounts are independent kernel state and are checked separately.
