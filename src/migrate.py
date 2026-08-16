#!/usr/bin/env python3
"""migrate.py <name> [--dry-run] — copy one container's docker volumes from
the pre-rebrand `dev-agent-<name>_*` prefix to the current `djinn-<name>_*`
prefix (docker-dev → brassbottle; the CLI brand dev-agent → djinn).

Volumes are COPIED, never moved: the old `dev-agent-<name>_*` volumes are
left exactly as they are, so a failed or partial migration never loses data
and the migration can simply be rerun. Only after you've confirmed the
container is healthy on the new prefix (`./djinn up <name>`) would you
`docker volume rm` the old ones by hand.

Usage:
    python3 src/migrate.py <name> [--dry-run]
    ./djinn migrate <name> [--dry-run]

Every docker/subprocess call goes through _run(), so the plan (what WILL run)
and the outcome (what DID run, and its result) are both logged to stdout —
and a failure always shows the exact command that failed. --dry-run prints
the plan and executes nothing.

Stdlib only (matches wire_plugins.py / manifest.py).
"""

import argparse
import subprocess
import sys

OLD_PREFIX = "dev-agent-"
NEW_PREFIX = "djinn-"
BUSYBOX_IMAGE = "busybox"


class MigrateError(Exception):
    """Fatal migration error; main() prints it as 'Error: …' and exits 1."""


def _run(cmd):
    """Single choke point for every subprocess call — logs the exact command
    about to run (the outbound half of the boundary log) so both the plan
    and any failure are reconstructable from stdout alone."""
    print(f"  $ {' '.join(cmd)}")
    return subprocess.run(cmd, capture_output=True, text=True)


def list_old_volumes(name):
    """Old volumes for <name>: `docker volume ls` filtered to the
    dev-agent-<name>_ prefix, sorted for deterministic output."""
    result = _run(["docker", "volume", "ls", "--format", "{{.Name}}"])
    if result.returncode != 0:
        raise MigrateError(f"docker volume ls failed: {result.stderr.strip()}")
    prefix = f"{OLD_PREFIX}{name}_"
    return sorted(v for v in result.stdout.splitlines() if v.startswith(prefix))


def target_name(old_name, name):
    """dev-agent-<name>_<suffix> -> djinn-<name>_<suffix>."""
    prefix = f"{OLD_PREFIX}{name}_"
    suffix = old_name[len(prefix):]
    return f"{NEW_PREFIX}{name}_{suffix}"


def plan_migration(name):
    """[(old, target), ...] for every old volume this container has. Empty
    means nothing to migrate — the caller turns that into a hard error."""
    return [(old, target_name(old, name)) for old in list_old_volumes(name)]


def volume_exists(vol):
    result = _run(["docker", "volume", "inspect", vol])
    return result.returncode == 0


def stop_old_container(name):
    """Best-effort: the old container may not exist or may already be
    stopped, and either is fine — only an unexpected docker failure would
    be worth surfacing, and `docker stop` on a missing container just
    returns nonzero with a message, so this never raises."""
    old_container = f"{OLD_PREFIX}{name}"
    print(f"Stopping old container {old_container} (if running)...")
    result = _run(["docker", "stop", old_container])
    if result.returncode == 0:
        print(f"  done: {old_container} stopped")
    else:
        print(f"  (skipped: {old_container} not running or not found)")


def copy_volume(old, target):
    """docker volume create <target>, then copy old's contents into it via a
    throwaway busybox container. Raises with the failing command's stderr on
    either step; the old volume is never touched."""
    print(f"Copying {old} -> {target} ...")
    created = _run(["docker", "volume", "create", target])
    if created.returncode != 0:
        raise MigrateError(f"docker volume create {target} failed: {created.stderr.strip()}")
    copy_cmd = [
        "docker", "run", "--rm",
        "-v", f"{old}:/from:ro",
        "-v", f"{target}:/to",
        BUSYBOX_IMAGE, "sh", "-c", "cp -a /from/. /to/",
    ]
    copied = _run(copy_cmd)
    if copied.returncode != 0:
        raise MigrateError(
            f"copy failed ({' '.join(copy_cmd)}): {copied.stderr.strip()}"
        )
    print(f"  done: {old} -> {target}")


def print_plan(name, mapping):
    print(f"Found {len(mapping)} old volume(s) for '{name}':")
    for old, target in mapping:
        print(f"  {old} -> {target}")


def print_dry_run(name, mapping):
    print("\n--dry-run: plan only, nothing executed.")
    print(f"  $ docker stop {OLD_PREFIX}{name}")
    for old, target in mapping:
        print(f"  $ docker volume create {target}")
        print(
            "  $ docker run --rm "
            f"-v {old}:/from:ro -v {target}:/to {BUSYBOX_IMAGE} "
            'sh -c "cp -a /from/. /to/"'
        )


def migrate(name, dry_run):
    """Run the full migration for <name>. Returns 0/1 (the process exit
    code); never raises MigrateError itself (main() is the sole catcher)."""
    try:
        mapping = plan_migration(name)
    except MigrateError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    if not mapping:
        print(
            f"Error: no volumes found with prefix '{OLD_PREFIX}{name}_' — nothing to migrate.",
            file=sys.stderr,
        )
        return 1

    print_plan(name, mapping)

    if dry_run:
        print_dry_run(name, mapping)
        return 0

    stop_old_container(name)

    copied = 0
    skipped = 0
    try:
        for old, target in mapping:
            if volume_exists(target):
                print(f"  ⚠ {target} already exists — skipping (never overwriting)")
                skipped += 1
                continue
            copy_volume(old, target)
            copied += 1
    except MigrateError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    print(f"\nDone: {copied} volume(s) copied, {skipped} skipped (target already existed).")
    print("Old volumes were kept untouched — remove them yourself once you've")
    print(f"confirmed the container is healthy: docker volume rm {' '.join(old for old, _ in mapping)}")
    print("Next steps:")
    print(f"  ./djinn up {name}   (the container joins djinn-net on this next up)")
    return 0


def main(argv):
    ap = argparse.ArgumentParser(
        description="Copy a container's docker volumes from the dev-agent- prefix to djinn-."
    )
    ap.add_argument("name", help="container name (the containers/<name>.yml manifest name)")
    ap.add_argument("--dry-run", action="store_true", help="print the plan; execute nothing")
    args = ap.parse_args(argv)
    return migrate(args.name, args.dry_run)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
