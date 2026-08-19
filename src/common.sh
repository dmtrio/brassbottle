#!/bin/bash
# common.sh — shared host-side config. NOT run directly; sourced by the repo's
# scripts (up.sh, down.sh, run-*.sh, update-agent-keys.sh). Resolves where
# secrets, keys, and artifacts live ("the djinn home") with two overrides,
# so a fresh clone is self-contained but your own setup keeps working:
#   1. ./.env at the repo root (gitignored) — set DJINN_HOME / RULES_PATH there
#   2. the DJINN_HOME environment variable (DEV_AGENT_HOME still honored, compat)
# Default: a gitignored ./.djinn inside this repo — or, on a repo that predates
# the rebrand, the existing ./.dev-agent (auto-detected; nothing is moved for you).

# Pure config resolution — no filesystem side effects, so sourcing this on a
# usage/error path (e.g. `./up.sh` with no args) creates nothing. Callers
# `mkdir -p "$BASE_PATH"` themselves once they've decided to proceed.
# This file lives in src/; the repo root (where ./.env and ./.djinn live)
# is its PARENT directory.
CDD_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
if [ -f "$CDD_ROOT/.env" ]; then
    # Disable errexit around the source: a failing line INSIDE ./.env would
    # otherwise trip the caller's set -e and abort silently before we report
    # it. Save and RESTORE the caller's exact errexit state (don't force it
    # on), then fail loud on a broken ./.env — a real config error.
    case $- in *e*) _had_e=1;; *) _had_e=0;; esac
    set +e; . "$CDD_ROOT/.env"; _env_rc=$?
    [ "$_had_e" = 1 ] && set -e
    [ "$_env_rc" -eq 0 ] || { echo "common.sh: ./.env exited non-zero ($_env_rc) — check $CDD_ROOT/.env" >&2; exit 1; }
    unset _had_e _env_rc
fi
if [ -n "${DJINN_HOME:-}" ]; then
    BASE_PATH="$DJINN_HOME"
# rebrand-transitional: DEV_AGENT_HOME compat — delete this branch (and the
# ./.dev-agent auto-detect below) once every container's ./.env has moved to
# DJINN_HOME.
elif [ -n "${DEV_AGENT_HOME:-}" ]; then
    BASE_PATH="$DEV_AGENT_HOME"
elif [ ! -d "$CDD_ROOT/.djinn" ] && [ -d "$CDD_ROOT/.dev-agent" ]; then
    # rebrand-transitional: default chosen, no ./.djinn yet, but a
    # pre-rebrand ./.dev-agent exists — keep using it rather than silently
    # starting a second, empty home.
    BASE_PATH="$CDD_ROOT/.dev-agent"
    echo "common.sh: using existing $BASE_PATH (consider: mv .dev-agent .djinn)" >&2
else
    BASE_PATH="$CDD_ROOT/.djinn"
fi

# Where container manifests are read from (up.sh, allow-egress.sh). Same
# override philosophy as RULES_PATH: an explicit CONTAINERS_PATH (env or ./.env)
# wins; otherwise prefer a per-setup $BASE_PATH/containers when it exists — so
# your real, semi-private manifests (private repo URLs, LAN subnets, identity
# naming) live OUTSIDE this repo, e.g. as their own private git repo at
# ~/djinn/containers — and fall back to the repo's containers/ (which ships
# only TEMPLATE.yml). A [ -d ] read only; still no filesystem side effects.
# CONTAINERS_BUNDLED marks the last case — the fallback lives INSIDE this repo,
# so up.sh must never `git pull` it (that would pull brassbottle). The flag is
# set where the fallback is chosen, the same way RULES_BUNDLED is, because a
# post-hoc path comparison misfires through a symlink.
CONTAINERS_BUNDLED=0
if [ -z "${CONTAINERS_PATH:-}" ]; then
    if [ -d "$BASE_PATH/containers" ]; then
        CONTAINERS_PATH="$BASE_PATH/containers"
    else
        CONTAINERS_PATH="$CDD_ROOT/containers"
        CONTAINERS_BUNDLED=1
    fi
fi

# Container naming: the single source of truth for the current and
# pre-rebrand prefixes, so up.sh/down.sh/bin/allow-egress.sh build and
# resolve container names off one pair of vars instead of each hand-typing
# "djinn-"/"dev-agent-" (partial consolidation — src/entrypoint.sh,
# src/tmux-notify.sh, and migrate.py are baked/Python and stay as they are).
DJINN_CTR_PREFIX="djinn-"
# rebrand-transitional: the pre-rebrand container prefix (docker-dev /
# dev-agent). Only bin/allow-egress.sh still needs it, to resolve a real
# not-yet-migrated container. Delete once every container has been re-upped.
OLD_CTR_PREFIX="dev-agent-"
