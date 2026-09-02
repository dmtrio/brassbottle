# Disk

Docker keeps images, container writable layers, volumes, and build cache in one
virtual disk. When it fills, pulls and builds fail with `no space left on
device`. For djinn, the space is almost always in volumes, not images.

## Read the numbers first

```bash
docker system df
```

`SIZE` de-duplicates shared layers, so it is the true footprint — unlike
`docker images`, which double-counts. `RECLAIMABLE` is what pruning would
actually free; a row showing `0B` cannot be improved. Docker Desktop's disk
panel shows a per-row bar (`X GB / Y GB in use`); that is one row's breakdown,
not the disk limit. The real ceiling is Settings → Resources → Virtual disk
limit.

## Images: many tags, shared layers

`up.sh` sets `IMAGE_TAG="$NAME"`, so every bottle gets its own `djinn:<bottle>`
tag and the tag count tracks the bottle count. Tags are free; layers cost disk,
and layers are shared.

`AGENTS_ENABLED` and `PLUGINS_ENABLED` are declared near the top of the
`Dockerfile` but not *referenced* until the plugin install step. Cache diverges
only from the first reference onward, so base, apt, and common tooling layers
are stored once for all bottles.

**Keep it that way.** Referencing either ARG earlier forks every base layer per
bottle and multiplies image disk by the number of distinct tool combinations.

The upshot: `Images` normally reports `0B` reclaimable, because unused tags
share all their layers with running bottles. Image pruning is not where space
comes from.

## Volumes: where space accumulates

Every bottle owns volumes prefixed `djinn-<bottle>_`:

- `workspace`, `gh-auth` and `ssh-host-keys` from
  `compose/docker-compose.local.yml` (pinned in `STATIC_COMPOSE_VOLUME_NAMES`);
  `ssh-host-keys` is tiny and holds the bottle's sshd identity — deleting
  it makes the jump refuse the bottle with a changed-host-key warning,
- one per enabled agent, from `state_dirs:` in `agents/<name>/agent.yml`,
- any declared by an enabled plugin's `volumes:`.

Volumes outlive containers, and `./djinn down` without `--purge` leaves them.
Retiring or renaming a bottle strands the whole set — a rename leaves a full
parallel copy under the old prefix.

```bash
docker system df -v | awk '/^VOLUME NAME/,0' | sort -k3 -h
```

`LINKS 0` means unreferenced, **not** disposable: a stopped bottle's workspace
shows zero links while holding the only copy of unpushed work.

| Suffix | Holds | Risk |
|---|---|---|
| `*-auth`, `*-state`, `*-cache` | credentials, caches | low — worst case, re-login |
| `_ssh-host-keys` | the bottle's sshd identity | low — regenerated on next `up`, then `ssh-keygen -R` on the jump |
| `_workspace` | `repos/` and `worktrees/` | **high — may be the only copy of a branch** |

## Audit a workspace volume before deleting it

Never delete a `_workspace` volume on size or link count alone. Mount it and
ask git.

Mount at `/workspace`, not an arbitrary path — a worktree's `.git` file stores
an **absolute** path to its parent repo, so mounting elsewhere gives
`fatal: not a git repository`. `alpine` has no git, and the volume's UID will
not match, so install git and relax ownership:

```bash
docker run --rm -v <volume>:/workspace alpine sh -c '
  apk add --no-cache git >/dev/null 2>&1
  git config --global --add safe.directory "*"
  cd /workspace
  for r in main repos/*/ worktrees/*/ worktrees/*/*/; do
    [ -e "$r/.git" ] || continue
    echo "== $r"
    git -C "$r" status --porcelain
    git -C "$r" log --branches --not --remotes --oneline
    git -C "$r" stash list
  done'
```

The globs cover both layouts: `main/` with flat worktrees (pre-v2), and
`repos/<name>/` with `worktrees/<repo>/<branch>/` (current).

Reading it:

- `==` header with nothing under it — clean.
- `??` — untracked. `.claude/`, `.serena/`, `.mcp.json` are scaffolding; source
  files are not.
- Bare hashes — commits that reached no remote.
- `stash@{0}` — stashed work (rare; the workspace contract bans `git stash`).
- **No `==` headers at all** — different layout. Look before concluding it is
  empty: `ls -la /workspace; find /workspace -maxdepth 4 -name .git`.

## Rescue, then delete

Copy anything worth keeping out first. Bind-mount a **host** path — `/artifacts`
is a container path and Docker rejects it with `mounts denied`. Derive the host
path from inside a bottle by prepending the `/run/host_mark` root to field 4:

```bash
grep -E " /artifacts " /proc/self/mountinfo
```

```bash
docker run --rm -v <volume>:/workspace -v "<host-artifacts-path>":/out alpine \
  tar czf /out/<volume>.tgz -C /workspace .
```

The tarball lands on the host disk, not the Docker virtual disk, so it does not
compete for the space being freed. Then remove by name:

```bash
docker volume rm <volume> [<volume> ...]
```

Prefer naming volumes over filtering: `docker volume prune` and
`docker system prune --volumes` take every unreferenced volume, including
stopped bottles you still want.

## Reclaiming the rest

- Build cache: `docker builder prune -a` — safe, costs a slower next build.
- Unused images: `docker image prune -a` — usually frees nothing, per above.
- Container writable layers grow with use and are not prunable; a `./djinn up`
  recreate resets them.

Freed space returns to the virtual disk immediately, but the host does not
shrink the backing file on its own. On Docker Desktop, Settings → Resources →
Clean / Purge data compacts it. That only matters when the host itself is short
on space.
