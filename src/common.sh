#!/bin/bash
# common.sh — shared host-side config. NOT run directly; sourced by the repo's
# scripts (up.sh, down.sh, run-*.sh, update-agent-keys.sh). Resolves where
# secrets, keys, and artifacts live ("the djinn home") with two overrides,
# so a fresh clone is self-contained but your own setup keeps working:
#   1. ./.env at the repo root (gitignored) — set DJINN_HOME / RULES_PATH there
#   2. the DJINN_HOME environment variable
# Default: a gitignored ./.djinn inside this repo.

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
else
    BASE_PATH="$CDD_ROOT/.djinn"
fi

# Where bottles are read from (up.sh, allow-egress.sh). A BOTTLE is the
# manifest — the YAML that declares a container. The container is the running
# thing (DJINN_CTR_PREFIX below); one word for both is what this naming fixes.
#
# Same override philosophy as RULES_PATH: an explicit BOTTLES_PATH (env or
# ./.env) wins; otherwise prefer a per-setup $BASE_PATH/bottles when it exists
# — so your real, semi-private bottles (private repo URLs, LAN subnets,
# identity naming) live OUTSIDE this repo, e.g. as their own private git repo
# at ~/git/djinn-bottles — and fall back to the repo's bottles/ (which ships
# only TEMPLATE.yml). A [ -d ] read only; still no filesystem side effects.
#
# CONTAINERS_PATH / $BASE_PATH/containers are the pre-rename spellings, still
# honored so a live ./.env keeps working. Each warns once, naming the path it
# actually used — silently accepting the old name is how a stale directory gets
# read for months. Retire both branches when this prints nothing:
#     grep -n 'CONTAINERS_PATH' ~/git/brassbottle/.env
# BOTTLES_BUNDLED marks the in-repo fallback — it lives INSIDE brassbottle, so
# up.sh must never `git pull` it (that would pull brassbottle). The flag is set
# where the fallback is chosen, the same way RULES_BUNDLED is, because a
# post-hoc path comparison misfires through a symlink.
BOTTLES_BUNDLED=0
if [ -n "${BOTTLES_PATH:-}" ]; then
    :
elif [ -n "${CONTAINERS_PATH:-}" ]; then
    BOTTLES_PATH="$CONTAINERS_PATH"
    echo "common.sh: CONTAINERS_PATH is deprecated — rename it to BOTTLES_PATH in ./.env (using $BOTTLES_PATH)" >&2
elif [ -d "$BASE_PATH/bottles" ]; then
    BOTTLES_PATH="$BASE_PATH/bottles"
elif [ -d "$BASE_PATH/containers" ]; then
    BOTTLES_PATH="$BASE_PATH/containers"
    echo "common.sh: $BASE_PATH/containers is deprecated — rename it to $BASE_PATH/bottles (using $BOTTLES_PATH)" >&2
else
    BOTTLES_PATH="$CDD_ROOT/bottles"
    BOTTLES_BUNDLED=1
fi

# Container naming: the single source of truth for the prefix, so
# up.sh/down.sh/bin/allow-egress.sh build and resolve container names off one
# var instead of each hand-typing "djinn-" (partial consolidation —
# src/entrypoint.sh and src/tmux-notify.sh are baked and stay as they are).
DJINN_CTR_PREFIX="djinn-"
