# Singleton artifact backups

One Dockerized restic service per djinn installation snapshots every bottle's
artifact outbox and browser exchange directory. The service is independent of
individual bottle containers and survives across `djinn up` / `djinn down`
cycles for any bottle.

## Lifecycle

```text
./djinn backup start    # create layout, write compose overlay, build/start container
./djinn backup stop     # stop the singleton backup container
./djinn backup status   # running / stopped (exit 0 only when running)
./djinn backup logs     # container logs (add -f to follow)
./djinn backup snapshots
./djinn backup check
./djinn backup restore <snapshot-id> --target <host-path>
```

`start` is idempotent: repeated runs update the same compose project and
container for this `DJINN_HOME`. Only one backup container should exist per djinn
home. Different `DJINN_HOME` paths on the same Docker host get distinct
compose project and container names (derived from a short hash of the resolved
home path), so one installation cannot silently replace another's backup service.

`snapshots` and `check` require the backup container to be **running** — they
use `docker compose exec` against the long-running daemon. If the service is
stopped, the CLI reports a clear error suggesting `./djinn backup start`.
`restore` uses `docker compose run --rm` and works without the daemon (useful
during disaster recovery).

On first start the service initializes an empty restic repository before the
first scheduled backup. Concurrent starters race safely — a peer that wins the
init is treated as success.

## Paths and isolation

All paths are relative to `DJINN_HOME` (defaults to `./.djinn` beside the
brassbottle checkout).

| Path | Role |
|------|------|
| `artifacts/` | Live agent outbox (read-only mount into backup container) |
| `browser-tmp/` | Live browser exchange dir (read-only mount) |
| `backups/restic-repo/` | Restic repository (read/write, backup container only) |
| `backups/restic-password` | Restic password file (mode 600, backup container only) |
| `compose/backup.yml` | Generated compose overlay (do not hand-edit) |

Bottle compose **never** mounts `backups/`, the password file, or the backup
container. Agent containers cannot read or modify the repository.

Inside the backup container:

- `/sources/artifacts` — read-only artifacts root
- `/sources/browser-tmp` — read-only browser-tmp root
- `/repo` — restic repository
- `/run/secrets/restic-password` — password file (read-only)

## Schedule and retention

Defaults (override on the **host** via environment before `./djinn backup start`;
`start` regenerates `compose/backup.yml` on every run, so hand-editing that file
does not persist):

| Variable | Default | Meaning |
|----------|---------|---------|
| `DJINN_BACKUP_INTERVAL_SECONDS` | `600` | Seconds between backup runs |
| `DJINN_BACKUP_PRUNE_INTERVAL_SECONDS` | `86400` | Seconds between forget/prune cycles |
| `DJINN_BACKUP_RETENTION_HOURLY` | `48` | Hourly snapshots to keep |
| `DJINN_BACKUP_RETENTION_DAILY` | `30` | Daily snapshots to keep |

Each backup run tags snapshots `scheduled`. On the prune schedule the service
runs `restic forget` with the retention policy, prunes unreachable data, then
runs `restic check` to verify repository integrity. Check failures are logged
and surfaced as errors.

The first forget/prune cycle runs only after `PRUNE_INTERVAL_SECONDS` elapses
from daemon start — not immediately on container start.

Interval and retention values must be positive integers. Invalid values cause
the daemon to exit with a visible error instead of crashing or busy-looping.

Boundary logs (`backup run ok`, `backup forget ok`, `backup check ok`, etc.)
record stage, status, duration, and aggregate counts — never file contents or
credentials.

## Restore workflow

Restores always target an explicit host directory **outside** the live
`artifacts/` and `browser-tmp/` trees and **outside** backup internals
(`backups/`, the restic repository, password file, and generated compose overlay).
The operator CLI rejects targets that equal, contain, or are contained by any of
those paths, rejects an existing non-directory target (file, symlink, etc.), and
rejects **non-empty** existing directories.

`restic restore` merges into the target directory: files whose paths appear in
the snapshot overwrite any colliding files already present. Reusing a directory
that still holds unrelated files can destroy them. Use a fresh or empty scratch
directory for every restore.

```text
./djinn backup start          # required before snapshots/check
./djinn backup snapshots
./djinn backup restore latest --target /tmp/artifact-restore
```

Inspect the restored tree, then copy needed files back manually. Never restore
directly over live artifact data.

## Docker dependency

The backup service requires Docker and Docker Compose on the host. `start` builds
`backup/Dockerfile` (python:3.12-alpine + restic) locally with an image tag
scoped to this `DJINN_HOME`.

CI builds this image on every PR and runs a Docker/restic integration smoke
test (init, backup, snapshots, check, restore). Local development without Docker
can still run the Python unit tests; end-to-end backup/restore requires Docker
on the host.

## Disaster recovery verification

Periodically confirm backups are recoverable:

1. `./djinn backup status` — container running
2. `./djinn backup snapshots` — recent `scheduled` snapshots exist
3. `./djinn backup check` — repository integrity passes
4. Restore to an **empty** scratch directory and spot-check files:

   ```text
   ./djinn backup restore latest --target /tmp/dr-verify
   diff -r /tmp/dr-verify/sources/artifacts "$DJINN_HOME/artifacts"  # expect match for unchanged files
   ```

Keep `backups/restic-password` and `backups/restic-repo/` together in your
host backup strategy. Losing the password makes the repository unreadable.
