# Examples

The examples use one root and assume the root package is installed. All
commands run as the same ordinary user.

```sh
ROOT="$HOME/.local/state/git-viewspace/example"
```

## Initialize from a clone

```sh
git-viewspace --root "$ROOT" init \
  --git-name "VIEWSPACE User" \
  --git-email user@example.invalid \
  --clone https://example.invalid/project.git \
  --git-arg-branch main

git-viewspace --root "$ROOT" start
git-viewspace --root "$ROOT" status
```

If clone authentication fails, configure ordinary Git authentication outside
git-viewspace or prepare a clean local checkout and use `--move`:

```sh
git-viewspace --root "$ROOT" init \
  --git-name "VIEWSPACE User" \
  --git-email user@example.invalid \
  --move "$HOME/prepared-project"
```

## Read-only view

Create and start a view in one request:

```sh
git-viewspace --root "$ROOT" view init-and-start readonly docs --at main
ls "$ROOT/views/docs/mnt"
```

Switch it to another commit:

```sh
git-viewspace --root "$ROOT" view stop docs
git-viewspace --root "$ROOT" view switch docs --to main~1
git-viewspace --root "$ROOT" view start docs
```

Remove it:

```sh
git-viewspace --root "$ROOT" view stop docs
git-viewspace --root "$ROOT" view destroy docs
```

## Writable view using primitive commands

The initial commit and owned branch tip must be equal:

```sh
git-viewspace --root "$ROOT" view init readonly work --at main
git-viewspace --root "$ROOT" view init overlay work --branch main
git-viewspace --root "$ROOT" view start work
```

Make ordinary changes only through the public mount:

```sh
VIEW="$ROOT/views/work/mnt"
printf '%s\n' 'new content' >"$VIEW/new-file.txt"
mv "$VIEW/old-name.txt" "$VIEW/new-name.txt"
rm "$VIEW/obsolete.txt"
```

Commit and move the view to the result:

```sh
git-viewspace --root "$ROOT" view stop work
git-viewspace --root "$ROOT" view sync work
git-viewspace --root "$ROOT" view stage work
git-viewspace --root "$ROOT" view commit work --message "Update project files"
git-viewspace --root "$ROOT" view clean work
git-viewspace --root "$ROOT" view switch work --next
git-viewspace --root "$ROOT" view start work
```

## Writable view using compound commands

Creation can be one request:

```sh
git-viewspace --root "$ROOT" view init-and-start overlay work \
  --from-branch main --new-branch runner-work
```

After modifying `$ROOT/views/work/mnt`, advance it in one request:

```sh
git-viewspace --root "$ROOT" view advance work \
  --message "Update project files"
```

`view advance` accepts a started or stopped view. On success the view is
started at the new commit with a fresh empty upper generation.

## Merge an attempt into its parent

Stop the completed attempt view and read its exact commit from `view status`,
then merge the branch without changing or deleting that view:

```sh
git-viewspace --root "$ROOT" view stop work
ATTEMPT_TIP=0123456789012345678901234567890123456789
git-viewspace --root "$ROOT" branch merge runner-work \
  --into main \
  --ensure-attempt-tip "$ATTEMPT_TIP" \
  --message "Merge runner-work" \
  --checked-out-destination detach-if-clean
```

The final option is needed only when `main` is the managed checkout. It proves
that checkout is clean and exact, then detaches it without rewriting its files
or index. The attempt branch still points to `$ATTEMPT_TIP`, and `work` may be
restarted. If the merge conflicts, both branch refs and checkout attachment
remain unchanged; JSON mode reports the bounded repository-relative conflict
list under `error.data.conflict_paths_rr`. Retrying the same command after a
lost successful response does not create a second merge commit.

## Inspect durable progress

```sh
git-viewspace --root "$ROOT" view status work
git-viewspace --root "$ROOT" --json view status work
```

If an operation stops after sync, stage, or commit, status shows that durable
phase. Retry the corresponding primitive or `view advance`; do not edit the
managed checkout to repair it manually.

## Discard writable changes

Stop and clean a view to throw away its current overlay changes:

```sh
git-viewspace --root "$ROOT" view stop work
git-viewspace --root "$ROOT" view clean work
```

Convert a clean writable view back to read-only:

```sh
git-viewspace --root "$ROOT" view deinit overlay work
```

Destroy dirty state only when loss is intentional:

```sh
git-viewspace --root "$ROOT" view stop work
git-viewspace --root "$ROOT" view destroy work --discard
```

## Stop and recover the root

```sh
git-viewspace --root "$ROOT" stop
git-viewspace --root "$ROOT" status
git-viewspace --root "$ROOT" start
```

Root stop removes all mounts and the git-viewspace-fs child but preserves desired
view state. The next start restores views that were durably marked started.

For unattended operation, run `start --foreground` under a service supervisor
and use the explicit stop command during shutdown. See [Running the daemon as
a service](service.md).
