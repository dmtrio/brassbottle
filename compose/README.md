# compose

Docker Compose files are implementation detail for `./djinn up`. Users normally
edit bottles, not these files.

## Files

- `docker-compose.local.yml` is the base container definition.
- `docker-compose.ssh.yml` is added when the bottle has an `ssh:` section
  (Mac Remote-SSH publishing).

`up.sh` builds the compose command from the bottle-derived settings:

```text
docker-compose.local.yml
+ docker-compose.ssh.yml   when ssh: is enabled (Mac direct access)
```

Plugin-declared named volumes are generated into a temporary overlay under the
runtime home and included during `up`.

## What Lives Here

The compose files define container mounts, auth volumes, published ports, and
runtime environment variables. Validation code in `src/manifest.py` and tests
pin the static volume names and mount targets so plugins cannot silently collide
with core mounts.

If you are changing user-facing behavior, update the bottle schema and docs
first. Change compose files when the container runtime shape itself changes.
