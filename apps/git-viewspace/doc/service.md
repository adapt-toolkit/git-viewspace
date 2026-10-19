# Running the daemon as a service

`git-viewspace` runs one daemon per initialized root. For interactive use,
`git-viewspace --root <root> start` launches it in the background. A service manager
should instead use `start --foreground` so the daemon process remains the
supervised process.

## Minimal systemd example

The following is an example to adapt, not an installed unit. Replace the user,
paths, and unit name for each managed root.

```ini
[Unit]
Description=Git Viewspace manager for project
After=local-fs.target

[Service]
Type=simple
User=example
Group=example
ExecStart=/home/example/.local/lib/git-viewspace/bin/git-viewspace --root /home/example/.local/state/git-viewspace/project start --foreground
ExecStop=/home/example/.local/lib/git-viewspace/bin/git-viewspace --root /home/example/.local/state/git-viewspace/project stop
Restart=on-failure
RestartSec=2s
TimeoutStartSec=30s
TimeoutStopSec=30s
KillMode=control-group

[Install]
WantedBy=multi-user.target
```

The unit must run as the same non-root user that initialized the root. Its
environment must allow the manager to find Git and must satisfy the
non-interactive privilege policy described in [Installation and host
prerequisites](installation.md).

Do not add service hardening settings that create a private mount namespace or
forbid privilege transitions. Public views must be visible outside the
service, and the daemon must be able to invoke its fixed `sudo -n` operations.

## Startup and restart behavior

Daemon startup validates durable state and recognized runtime residue before
admitting commands. It starts one persistent git-viewspace-fs child, then restores
views whose durable desired state is started.

If git-viewspace-fs exits while the daemon remains alive, the daemon starts a new
child and restores validated public views. If the daemon exits unexpectedly,
`Restart=on-failure` starts a new daemon, which reconciles the prior published
runtime identity and desired mounts.

Some partially published operations are intentionally not automatically
repairable. Repeated service restart is not a repair strategy when startup
reports inconsistent durable state. Stop the service, preserve the root, and
follow [Daemon and recovery](daemon.md#current-hardening-boundary).

## Shutdown

The service manager should invoke `git-viewspace stop`, not send an immediate kill.
Successful root shutdown:

1. stops command admission;
2. removes every public view mount;
3. unmounts the private git-viewspace-fs mount;
4. terminates the supervised child;
5. removes runtime files; and
6. releases the root and repository locks.

View definitions and desired-started flags remain durable. The next service
start restores views that were previously desired started.

If the orderly stop exceeds the configured timeout, systemd may terminate the
cgroup while mounts still exist. Treat that as an unclean interruption and
inspect `git-viewspace status` before restarting.

## Multiple repositories

Use one initialized root and one service instance per repository. Separate
roots may run concurrently as long as their repositories, sockets, storage,
and mountpoints are disjoint. The MVP does not provide one global daemon that
owns multiple roots.
