#!/bin/bash
# up.sh <name> — declaratively create or update an agent dev container from
# bottles/<name>.yml. Idempotent: edit the manifest, rerun, done.
#
# Kept:     the bottle (bottles/*.yml) and ~/djinn/secrets.env
# Derived:  ~/djinn/keys/<name>/ (recomposed every run), the container,
#           generated .mcp.json / dev.code-workspace / workspace AGENTS.md
# Survives: workspace volume (code), ~/djinn/artifacts/<name>/
#
# Requires: docker, yq (brew install yq / static binary on Linux), python3
# (stdlib only — owns ALL manifest validation/derivation via src/manifest.py
# and builds the wiring payload; yq only converts YAML→JSON. The wiring
# itself runs in-container via the baked-in src/wire_plugins.py).

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
. "$SCRIPT_DIR/src/common.sh"   # sources ./.env, sets BASE_PATH (the djinn home)

NAME="$1"
if [ -z "$NAME" ]; then
    echo "Usage: ./up.sh <name>    (reads $BOTTLES_PATH/<name>.yml)"
    echo "Manifests:"
    for f in "$BOTTLES_PATH"/*.yml; do
        [ -f "$f" ] || continue
        n=$(basename "$f" .yml)
        [ "$n" = "TEMPLATE" ] && continue
        printf "  %s\n" "$n"
    done
    exit 1
fi
# The container's full docker name (common.sh's DJINN_CTR_PREFIX — see
# src/common.sh — is the single source of truth for the prefix; down.sh and
# bin/allow-egress.sh build/resolve off the same var).
CNAME="$DJINN_CTR_PREFIX$NAME"

# Host python3 (stdlib-only, builds the wiring payload): prefer the SYSTEM
# interpreter over whatever shim leads $PATH — pyenv/homebrew pythons can be
# present-but-broken (dyld: library not loaded) in ways `command -v` cannot
# see, so each candidate must actually RUN. Override with PYTHON3=/path.
# Resolved BEFORE the manifest is read: src/pull_manifests.py runs next, and a
# manifest merged upstream only exists locally once that pull has happened.
# require_python3 (src/common.sh, sourced above) owns the candidate loop and
# the diagnostic; up.sh only decides what to do on failure (exit 1).
require_python3 || exit 1

# BOTTLES_PATH (resolved in common.sh) is where manifests live: the repo's
# bottles/ by default, or $BASE_PATH/bottles / a BOTTLES_PATH override
# when you keep them in a private repo outside this one.
#
# Fast-forward that checkout first, the way RULES_PATH is pulled below: once
# manifests live in their own repo, a merged bottle PR is invisible here until
# someone pulls by hand, and up.sh would apply the stale file without a word.
# Never fatal, and never pulls the bundled bottles/ (that would pull
# brassbottle) — src/pull_manifests.py owns those rules and prints which case
# it took.
# NB: an if, not ${BOTTLES_BUNDLED:+…} — the flag is the string "0" when
# unset-by-value, which :+ treats as set and would disable the pull outright.
BUNDLED_FLAG=""
if [ "${BOTTLES_BUNDLED:-0}" = 1 ]; then BUNDLED_FLAG="--bundled"; fi
# `|| true`, like the RULES_PATH pull below: main() returning 0 only covers
# failures INSIDE the module. A missing or unreadable src/pull_manifests.py, or
# any unhandled traceback, would exit non-zero and abort bring-up under set -e
# — the one thing this feature promises it can never do.
"$PYTHON3" "$SCRIPT_DIR/src/pull_manifests.py" "$BOTTLES_PATH" \
    --self "$SCRIPT_DIR" $BUNDLED_FLAG || true

MANIFEST="$BOTTLES_PATH/$NAME.yml"
[ -f "$MANIFEST" ] || { echo "Error: no manifest at $MANIFEST (cp $SCRIPT_DIR/bottles/TEMPLATE.yml $MANIFEST)"; exit 1; }
command -v yq >/dev/null || { echo "Error: yq required (brew install yq)"; exit 1; }

mkdir -p "$BASE_PATH"   # create the djinn home now that we're proceeding
SHARED_PATH="$BASE_PATH/shared"
SECRETS_FILE="$BASE_PATH/secrets.env"
[ -f "$SECRETS_FILE" ] || { touch "$SECRETS_FILE"; chmod 600 "$SECRETS_FILE"; }
. "$SECRETS_FILE"

# ── Read + validate manifest (src/manifest.py owns the rules) ────────────────
# yq only converts YAML→JSON here; every validation rule, default, and derived
# value lives in src/manifest.py (unit-tested table-driven — named errors
# instead of cryptic yq failures). Secret VALUES stay out of the call: it
# receives only the NAMES of the identity key vars that are set, plus
# NTFY_URL/NTFY_TOPIC which the manifest may route into the container.
# The manifest only receives NAMES of set values. Hybrid secret resolution
# needs to tell an explicit common default from an unset source, while secret
# values remain in this shell and reach the container through keyfiles.sh.
# The universe of legitimate secret sources is secrets.env itself, so scan the
# file's assigned names — NOT the whole shell environment (`compgen -v` would
# fold in PATH/HOME/USER/…, letting a typo'd source resolve to a non-secret
# value instead of hard-failing) — and keep the ones that are non-empty.
PRESENT_SECRET_VARS=""
for v in $(grep -oE '^[[:space:]]*(export[[:space:]]+)?[A-Za-z_][A-Za-z0-9_]*=' "$SECRETS_FILE" \
           | sed -E 's/^[[:space:]]*(export[[:space:]]+)?//; s/=$//' | LC_ALL=C sort -u); do
    if [ -n "${!v}" ]; then
        PRESENT_SECRET_VARS="${PRESENT_SECRET_VARS:+$PRESENT_SECRET_VARS }$v"
    fi
done
# The set of GH_TOKEN* var names present in secrets.env (NAMES only — values
# stay on the host). manifest.py validates every git.token / git.orgs.*.token
# against this list, hard-failing a manifest that names a token var that isn't
# set rather than silently falling back to the wrong identity.
GH_TOKEN_VARS=""
for v in $(compgen -v | grep -E '^GH_TOKEN' || true); do
    if [ -n "${!v}" ]; then GH_TOKEN_VARS="${GH_TOKEN_VARS:+$GH_TOKEN_VARS }$v"; fi
done
DERIVED=$(
    {
        yq -o=json -I=0 "$MANIFEST"
        for f in "$SCRIPT_DIR/plugins"/*/plugin.yml; do
            [ -e "$f" ] || continue
            # Each plugin is a directory: plugins/<name>/plugin.yml (+ optional
            # host-only run.sh). The plugin NAME is the parent dir name.
            # '!' = unreadable. manifest.py errors on it ONLY when the
            # manifest lists that plugin — a broken/WIP file in plugins/
            # must not block bring-up of containers that never use it.
            DOC=$(yq -o=json -I=0 "$f" 2>/dev/null) \
                && [ "$(printf '%s\n' "$DOC" | wc -l)" -eq 1 ] || DOC='!'
            printf '%s\t%s\n' "$(basename "$(dirname "$f")")" "$DOC"
        done
        echo "---agents---"
        for f in "$SCRIPT_DIR/agents"/*/agent.yml; do
            [ -e "$f" ] || continue
            DOC=$(yq -o=json -I=0 "$f" 2>/dev/null) \
                && [ "$(printf '%s\n' "$DOC" | wc -l)" -eq 1 ] || DOC='!'
            printf '%s\t%s\n' "$(basename "$(dirname "$f")")" "$DOC"
        done
    } | PRESENT_SECRET_VARS="$PRESENT_SECRET_VARS" GH_TOKEN_VARS="$GH_TOKEN_VARS" \
        SECRETS_FILE="$SECRETS_FILE" \
        GIT_NAME_DEFAULT="$(git config --global user.name 2>/dev/null || true)" \
        GIT_EMAIL_DEFAULT="$(git config --global user.email 2>/dev/null || true)" \
        NTFY_URL="${NTFY_URL:-}" NTFY_TOPIC="${NTFY_TOPIC:-}" \
        "$PYTHON3" "$SCRIPT_DIR/src/manifest.py" --derive
)
eval "$DERIVED"

# Per-bottle egress broker token: read host-side, inject as env at create time.
# Never bind-mount $BASE_PATH/run/ into a container — the queue must stay
# host-only. The token is visible to processes in the container; that is fine
# (filing is what request-egress does) and per-bottle tokens limit blast radius.
EGRESS_BROKER_TOKEN=""
if [ "$ENABLE_EGRESS_BROKER" = "true" ]; then
    EGRESS_BROKER_TOKEN="$("$PYTHON3" "$SCRIPT_DIR/src/egress_broker_host.py" \
        --base-path "$BASE_PATH" --ensure-bottle-token "$NAME")"
fi

# Resolve the container's default git token from the manifest's git.token (a
# secrets.env var NAME; manifest.py already checked it is set). Absent → keep
# GH_TOKEN as sourced from secrets.env, so manifests with no git.token keep the
# global machine-user token (backward compatible). This GH_TOKEN is what
# keyfiles.sh fans into every <agent>.env and the clone bootstrap hands to git.
if [ -n "$GIT_TOKEN_SOURCE" ]; then GH_TOKEN="${!GIT_TOKEN_SOURCE}"; fi

COMPOSE_FILES="-f $SCRIPT_DIR/compose/docker-compose.local.yml"
[ -n "$SSH_PORT" ] && COMPOSE_FILES="$COMPOSE_FILES -f $SCRIPT_DIR/compose/docker-compose.ssh.yml"

# Plugin-declared volumes (plugins/<name>/volumes:) ride in as one more overlay,
# the same mechanism as ssh — except this one is DERIVED per container from
# its manifest, which is what keeps compose/ free of any plugin's name.
# manifest.py renders the YAML (unit-tested); up.sh only places the file and
# adds the -f. Written under BASE_PATH, not the repo: it is per-container
# derived state, like keys/<name>/. Removed when no enabled plugin declares a
# volume, so de-listing the plugin really drops the mount on the next up.
PLUGIN_COMPOSE_FILE="$BASE_PATH/compose/$NAME.plugins.yml"
if [ -n "$PLUGIN_COMPOSE_YAML" ]; then
    mkdir -p "$BASE_PATH/compose"
    printf '%s\n' "$PLUGIN_COMPOSE_YAML" > "$PLUGIN_COMPOSE_FILE"
    COMPOSE_FILES="$COMPOSE_FILES -f $PLUGIN_COMPOSE_FILE"
else
    rm -f "$PLUGIN_COMPOSE_FILE"
fi

# Agent auth/state volumes (agents/*/agent.yml state_dirs:) are also derived
# per container and mounted as a compose overlay under BASE_PATH, for the same
# reasons as plugin volumes above.
AGENTS_COMPOSE_FILE="$BASE_PATH/compose/$NAME.agents.yml"
if [ -n "$AGENTS_COMPOSE_YAML" ]; then
    mkdir -p "$BASE_PATH/compose"
    printf '%s\n' "$AGENTS_COMPOSE_YAML" > "$AGENTS_COMPOSE_FILE"
    COMPOSE_FILES="$COMPOSE_FILES -f $AGENTS_COMPOSE_FILE"
else
    rm -f "$AGENTS_COMPOSE_FILE"
fi

# ── Compose derived credentials (keys/<name>/ is rebuilt from scratch) ───────
KEYS_PATH="$BASE_PATH/keys/$NAME"
mkdir -p "$KEYS_PATH"; chmod 700 "$KEYS_PATH"
rm -f "$KEYS_PATH"/*.env

# One COMPLETE env file per shim agent (Plugins v2 Phase 3 — common.env retired).
# Each shim (baked into the image; SHIM_AGENTS must match the Dockerfile loop)
# sources only its own <agent>.env, so that file carries everything the agent
# sees. The composition logic lives in src/keyfiles.sh (sourced here, and
# unit-tested by tests/bash.test.sh) so the real code is exercised in tests, not
# mirrored; up.sh only routes the derived vars (NAMES) into it — the ${!source}
# value lookups happen against the secrets.env this shell already sourced.
. "$SCRIPT_DIR/src/keyfiles.sh"
write_keyfiles "$KEYS_PATH" "$SHIM_AGENTS" "$PLUGIN_ENV_SECRETS" "$AGENT_SECRETS" "$GIT_ORG_TOKENS"

# ── Host paths + platform ─────────────────────────────────────────────────────
ARTIFACTS_PATH="$BASE_PATH/artifacts/$NAME"
mkdir -p "$ARTIFACTS_PATH"
BROWSER_TMP_PATH="$BASE_PATH/browser-tmp/$NAME"
mkdir -p "$BROWSER_TMP_PATH"
# Rules: RULES_PATH override (set in ./.env) → your existing $BASE_PATH/rules
# → the bundled repo rules. The bundled default makes a fresh clone runnable;
# point RULES_PATH at your own rules repo to override (the agent-conf usecase).
RULES_BUNDLED=0
if [ -z "${RULES_PATH:-}" ]; then
    if [ -d "$BASE_PATH/rules" ]; then RULES_PATH="$BASE_PATH/rules"
    else RULES_PATH="$SCRIPT_DIR/rules"; RULES_BUNDLED=1; fi
fi
[ -d "$RULES_PATH" ] || { echo "Error: RULES_PATH '$RULES_PATH' does not exist"; exit 1; }
# Resolve symlinks: Docker Desktop cannot use a symlink as a bind source
RULES_PATH="$(cd "$RULES_PATH" && pwd -P)"
# Keep an EXTERNAL rules repo current (merged rule PRs land here). Never pull
# the bundled copy — it lives inside THIS repo, so a pull would pull brassbottle.
# The flag is set where the fallback is chosen, so it's robust to symlinks that
# would make a post-hoc path comparison misfire.
[ "$RULES_BUNDLED" = 1 ] || git -C "$RULES_PATH" pull --ff-only -q 2>/dev/null || true

if [ "$(uname -s)" = "Linux" ]; then
    USER_UID="$(id -u)"; USER_GID="$(id -g)"
else
    USER_UID=1000; USER_GID=1000
fi

# ── SSH preflight check ──────────────────────────────────────────────────────
if [ -n "$SSH_PORT" ] && [ -z "${SSH_AUTHORIZED_KEY:-}" ]; then
    echo "Error: manifest has ssh.port but SSH_AUTHORIZED_KEY is missing from secrets.env"; exit 1
fi

# ── Shared network (all containers; single CIDR for VPN/tunnel targeting) ───
# One user-defined bridge with a stable subnet (override via DJINN_SUBNET in
# ./.env). Existing containers adopt it on their next recreate. The
# create/verify logic lives in src/ensure_net.py (unit-tested; see
# tests/test_ensure_net.py). up.sh only resolves the desired subnet and aborts
# on error.
DESIRED_SUBNET="${DJINN_SUBNET:-172.30.0.0/24}"
python3 "$SCRIPT_DIR/src/ensure_net.py" "$DESIRED_SUBNET" || exit 1

# ── Jump IP resolution (host-side; never fatal) ───────────────────────────────
# Must run AFTER ensure_net: jump_host.py's `ip` command prefers the LIVE
# djinn-net bridge subnet (mirroring cmd_start's own derivation) and needs the
# bridge to exist to read it. Never fatal — a bottle whose jump address can't
# be derived yet (fresh install, ./djinn jump start never run) just isn't
# jump-reachable this run, same quiet-degradation contract as the entrypoint's
# own no-keys case; init-firewall.sh and the summary/banner below only warn.
# common.sh sources ./.env WITHOUT exporting, so DJINN_SUBNET/DJINN_JUMP_IP
# must be forwarded explicitly here, same as jump.sh does.
#
# stderr is captured (not discarded) — a resolver failure used to disappear
# behind `2>/dev/null`, leaving the operator to guess why JUMP_IP came back
# empty. Surface every line the resolver printed, prefixed so it reads as
# this boundary's own diagnostic, then remove the temp file either way. The
# `if JUMP_IP=$(...); then … else …` shape (rather than `X=$(...); rc=$?`)
# is deliberate: under this script's `set -e`, a bare failed assignment from
# a command substitution aborts the script right there — wrapping it in the
# `if` is what keeps this resolution non-fatal, same as the `|| JUMP_IP=""`
# it replaces.
#
# mktemp itself must never be fatal here (this whole block is non-fatal by
# design) and must never leak into $BASE_PATH (~/djinn) on failure — an
# unwritable/missing BASE_PATH would otherwise abort under set -e right at
# this line, before the non-fatal `if` even runs. Use the system TMPDIR (no
# explicit template/dir) and fall back to /dev/null, which the read loop and
# `-s` test below both handle harmlessly (empty read, always non-"-s").
JUMP_IP_ERR="$(mktemp 2>/dev/null || echo /dev/null)"
if JUMP_IP="$(env DJINN_SUBNET="${DJINN_SUBNET:-}" DJINN_JUMP_IP="${DJINN_JUMP_IP:-}" DJINN_HOME="$BASE_PATH" "$PYTHON3" "$SCRIPT_DIR/src/jump_host.py" ip 2>"$JUMP_IP_ERR")"; then
    JUMP_IP_FAILED=false
else
    JUMP_IP_FAILED=true
    JUMP_IP=""
fi
if [ "$JUMP_IP_FAILED" = "true" ] || [ -s "$JUMP_IP_ERR" ]; then
    while IFS= read -r line; do
        echo "  jump: $line"
    done < "$JUMP_IP_ERR"
fi
[ "$JUMP_IP_ERR" = "/dev/null" ] || rm -f "$JUMP_IP_ERR"

# Scope the registry label to this DJINN_HOME. A shared Docker daemon may hold
# several installations; a jump must never list another installation's bottles.
JUMP_SCOPE_ERR="$(mktemp 2>/dev/null || echo /dev/null)"
if JUMP_REGISTRY_SCOPE="$(DJINN_HOME="$BASE_PATH" "$PYTHON3" "$SCRIPT_DIR/src/jump_host.py" scope 2>"$JUMP_SCOPE_ERR")"; then
    :
else
    JUMP_REGISTRY_SCOPE="unscoped"
    echo "  ⚠ jump: could not derive registry scope; bottle will stay unlisted" >&2
fi
[ "$JUMP_SCOPE_ERR" = "/dev/null" ] || rm -f "$JUMP_SCOPE_ERR"

# ── Jump public key (host-side; never fatal) ─────────────────────────────────
# The jump generates its own client key on first start and persists it under
# $DJINN_HOME/jump/ssh/. A public key is not a secret, so read it from there
# instead of asking the operator to paste `./djinn jump pubkey` output into
# secrets.env. A JUMP_AUTHORIZED_KEY set in secrets.env still wins (override
# for a jump that runs elsewhere) — jump_host.py prints the deprecation note.
# Skipped only when the bottle authorises no keys at all (remote.jump: false
# AND no published ssh:): a published-ssh bottle still appends the jump key
# (remote_access.py writes both in published mode), and a fresh install must
# not nag an opted-out bottle about a jump key it would never use.
JUMP_KEY_ERR="$(mktemp 2>/dev/null || echo /dev/null)"
if [ "$REMOTE_JUMP" != "true" ] && [ -z "$SSH_PORT" ]; then
    JUMP_KEY_FAILED=false
    JUMP_AUTHORIZED_KEY=""
elif JUMP_AUTHORIZED_KEY="$(env JUMP_AUTHORIZED_KEY="${JUMP_AUTHORIZED_KEY:-}" DJINN_HOME="$BASE_PATH" "$PYTHON3" "$SCRIPT_DIR/src/jump_host.py" authorized-key 2>"$JUMP_KEY_ERR")"; then
    JUMP_KEY_FAILED=false
else
    JUMP_KEY_FAILED=true
    JUMP_AUTHORIZED_KEY=""
fi
if [ "$JUMP_KEY_FAILED" = "true" ] || [ -s "$JUMP_KEY_ERR" ]; then
    while IFS= read -r line; do
        echo "  jump: $line"
    done < "$JUMP_KEY_ERR"
fi
[ "$JUMP_KEY_ERR" = "/dev/null" ] || rm -f "$JUMP_KEY_ERR"

# ── Jump reachability preflight (non-fatal) ───────────────────────────────────
# A quiet-degradation warning, not an error: the entrypoint and firewall
# already degrade gracefully when the address is missing (sshd with no INPUT
# rule yet). A missing KEY needs no line here — the resolver above already
# printed "no jump key yet … run: ./djinn jump start" for it.
if [ "$REMOTE_JUMP" = "true" ] && [ -z "$JUMP_IP" ]; then
    echo "  ⚠ jump: could not derive the jump address (DJINN_SUBNET/DJINN_JUMP_IP) — $NAME will not be jump-reachable"
fi
JUMP_REGISTRY_READY=false
if [ "$REMOTE_JUMP" = "true" ] && [ -n "$JUMP_IP" ] && [ -n "${JUMP_AUTHORIZED_KEY:-}" ]; then
    JUMP_REGISTRY_READY=true
fi

# ── Apply ─────────────────────────────────────────────────────────────────────
echo "Applying $MANIFEST → $CNAME"
REMOTE_SUMMARY=""
[ "$REMOTE_JUMP" = "true" ] && REMOTE_SUMMARY="jump"
REMOTE_SUMMARY="${REMOTE_SUMMARY:+$REMOTE_SUMMARY+}$REMOTE_SHELL"
[ -n "$REMOTE_NOTIFY" ]     && REMOTE_SUMMARY="${REMOTE_SUMMARY:+$REMOTE_SUMMARY+}$REMOTE_NOTIFY"
echo "  ports='${HOST_MCP_PORTS:-none}' egress='${EGRESS:-none}' plugins='${PLUGINS:-none}' remote='${REMOTE_SUMMARY:-none}' mem=$MEM_LIMIT"

# --project-directory pins relative paths in the compose files (notably the
# build context) to the repo root. Without it compose derives the project
# directory from the first -f file, i.e. compose/, and the build context
# resolves to compose/ — where there is no Dockerfile.
#
# NOTE: everything from here to the `docker compose` line is one command —
# a chain of env-var prefixes joined by trailing backslashes. Do not insert
# comments or blank lines inside it: a backslash-newline splices the next
# line in, so a comment silently swallows the whole prefix chain and compose
# runs with every one of these variables unset.
CONTAINER_NAME="$NAME" \
USER_UID="$USER_UID" USER_GID="$USER_GID" \
RULES_PATH="$RULES_PATH" \
GIT_USER_NAME="$GIT_USER_NAME" GIT_USER_EMAIL="$GIT_USER_EMAIL" \
AGENTS_ENABLED="$AGENTS_ENABLED" \
PLUGINS_ENABLED="$PLUGINS_ENABLED" \
HOST_MCP_PORTS="$HOST_MCP_PORTS" EXTRA_ALLOWED_DOMAINS="$EGRESS" \
ALLOWED_CIDRS="$EGRESS_CIDRS" \
ENABLE_EGRESS_BROKER="$ENABLE_EGRESS_BROKER" EGRESS_BROKER_TOKEN="$EGRESS_BROKER_TOKEN" \
KEYS_PATH="$KEYS_PATH" ARTIFACTS_PATH="$ARTIFACTS_PATH" BROWSER_TMP_PATH="$BROWSER_TMP_PATH" MEM_LIMIT="$MEM_LIMIT" \
SSH_PORT="$SSH_PORT" SSH_BIND="$SSH_BIND" SSH_AUTHORIZED_KEY="${SSH_AUTHORIZED_KEY:-}" \
  JUMP_AUTHORIZED_KEY="${JUMP_AUTHORIZED_KEY:-}" \
REMOTE_JUMP="$REMOTE_JUMP" REMOTE_SHELL="$REMOTE_SHELL" DJINN_JUMP_IP="$JUMP_IP" \
JUMP_REGISTRY_SCOPE="$JUMP_REGISTRY_SCOPE" \
JUMP_REGISTRY_READY="$JUMP_REGISTRY_READY" \
NTFY_URL="$CONTAINER_NTFY_URL" NTFY_TOPIC="$CONTAINER_NTFY_TOPIC" \
IMAGE_TAG="$NAME" \
docker compose -p "$CNAME" --project-directory "$SCRIPT_DIR" \
    $COMPOSE_FILES up -d --build

# ── Wait for entrypoint/firewall ──────────────────────────────────────────────
# Crash-loop detection compares against the restart count captured now (0 for
# a freshly (re)created container; the current value for a healthy no-op
# re-up). A rise DURING the wait = a crash loop this run — which also catches
# the SSH-missing-key case where 'firewall active' prints before the fatal
# exit (the marker alone would falsely read as success).
BASELINE_RESTARTS="$(docker inspect -f '{{.RestartCount}}' "$CNAME" 2>/dev/null || echo 0)"
i=0
READY=false
while [ $i -lt 24 ]; do
    STATUS="$(docker inspect -f '{{.State.Status}}' "$CNAME" 2>/dev/null || echo missing)"
    if [ "$STATUS" = "exited" ] || [ "$STATUS" = "missing" ] || [ "$STATUS" = "restarting" ]; then
        echo "Error: container failed to start. Logs:"
        docker logs "$CNAME" 2>&1 | tail -20
        exit 1
    fi
    RESTART_COUNT="$(docker inspect -f '{{.RestartCount}}' "$CNAME" 2>/dev/null || echo 0)"
    if [ "$RESTART_COUNT" -gt "$BASELINE_RESTARTS" ]; then
        echo "Error: container crash-loop detected (restarts rose to $RESTART_COUNT). Logs:"
        docker logs "$CNAME" 2>&1 | tail -20
        exit 1
    fi
    if docker logs "$CNAME" 2>&1 | grep -q "firewall active\|firewall DISABLED"; then
        # The marker persists in logs across restarts, so a crashing boot can
        # print it too. Confirm the container is actually STABLE: still running
        # and no new restart 2s later. A crash loop keeps incrementing, so this
        # catches a container that logged the marker then died.
        sleep 2
        CONFIRM_STATUS="$(docker inspect -f '{{.State.Status}}' "$CNAME" 2>/dev/null || echo missing)"
        CONFIRM_RESTARTS="$(docker inspect -f '{{.RestartCount}}' "$CNAME" 2>/dev/null || echo 0)"
        if [ "$CONFIRM_STATUS" != "running" ] || [ "$CONFIRM_RESTARTS" -gt "$BASELINE_RESTARTS" ]; then
            echo "Error: container crash-loop detected (unstable after readiness marker). Logs:"
            docker logs "$CNAME" 2>&1 | tail -20
            exit 1
        fi
        READY=true
        break
    fi
    sleep 5
    i=$((i + 1))
done

if [ "$READY" = "false" ]; then
    echo "Error: container did not reach readiness (timeout). Logs:"
    docker logs "$CNAME" 2>&1 | tail -20
    exit 1
fi

# Refresh the jump selector only after this bottle is confirmed running. The
# helper queries Docker on the HOST and writes into a directory mount, so the
# jump gets the update without Docker-socket access or a restart.
if [ -f "$BASE_PATH/compose/jump.yml" ]; then
    if ! DJINN_HOME="$BASE_PATH" "$PYTHON3" "$SCRIPT_DIR/src/jump_host.py" refresh; then
        echo "  ⚠ jump: picker registry refresh failed — the bottle is up; rerun './djinn jump refresh' to retry" >&2
    fi
fi

# ── Bootstrap workspace (idempotent; layout v2: /workspace/repos/<name>) ─────
# A v1 workspace (/workspace/main) cannot migrate in place — worktree metadata
# pins absolute paths — so refuse loudly instead of half-operating on a mixed
# layout. An EMPTY main/ (v1 entrypoint pre-create, never bootstrapped) is
# harmless: rmdir it and proceed. The refusal recommends the selective volume
# reset, NOT --purge: purge would also delete the auth volumes (agent logins,
# per-project state) that the reset — and the port shim below — preserve.
docker exec -u coder "$CNAME" bash -c 'rmdir /workspace/main 2>/dev/null || true'
if docker exec -u coder "$CNAME" bash -c '[ -e /workspace/main ]'; then
    echo "Error: this workspace uses layout v1 (/workspace/main). Layout v2 puts every repo under /workspace/repos/<name>."
    echo "Push every branch you care about, then reset the workspace volume (agent auth/state survives) and rerun:"
    echo "  ./djinn down $NAME && docker volume rm ${CNAME}_workspace && ./djinn up $NAME"
    exit 1
fi

if [ -n "$REPOS" ]; then
    # The bootstrap exec isn't shim-launched, so hand it the git tokens
    # explicitly for private-repo clones over HTTPS. git-credential-org (the
    # in-container helper) routes by owner, so it needs BOTH the default
    # GH_TOKEN and every per-org GH_TOKEN_<owner> in scope — otherwise a repo
    # owned by an org the default token can't reach would clone with the wrong
    # token and 404 (the exact failure this feature fixes). Tokens carry no
    # whitespace, so the unquoted -e assembly is safe. Name and URL ride as env
    # vars too — never spliced into the bash -c string.
    CLONE_ENV=""
    [ -n "${GH_TOKEN:-}" ] && CLONE_ENV="-e GH_TOKEN=$GH_TOKEN"
    while IFS=$'\t' read -r _owner _canon _src; do
        [ -n "$_owner" ] || continue
        CLONE_ENV="$CLONE_ENV -e $_canon=${!_src}"
    done <<EOF
$GIT_ORG_TOKENS
EOF
    while IFS=$'\t' read -r RNAME RURL; do
        [ -n "$RNAME" ] || continue
        docker exec $CLONE_ENV -e "REPO_NAME=$RNAME" -e "REPO_URL=$RURL" -u coder "$CNAME" bash -c \
            '[ -d "/workspace/repos/$REPO_NAME/.git" ] || git clone "$REPO_URL" "/workspace/repos/$REPO_NAME"' \
            || echo "WARNING: clone of '$RNAME' failed — private repo needs either GH_TOKEN in secrets.env (machine user must have repo access) or a one-time 'gh auth login' in the container"
        # Per-repo identity attribution: if this repo's OWNER has a git.orgs
        # override with a name/email, stamp it as the repo-local user.name/email
        # so commits to that owner's repos carry the right identity. Repos whose
        # owner has no override inherit the container-global identity from
        # entrypoint.sh. Owner = first path segment of the URL, for both
        # https://host/owner/repo and git@host:owner/repo (and creds@host) forms.
        REPO_OWNER="${RURL#*://}"; REPO_OWNER="${REPO_OWNER#*@}"
        REPO_OWNER="${REPO_OWNER#*[:/]}"; REPO_OWNER="${REPO_OWNER%%/*}"
        # case-fold to match GIT_ORG_IDENTITIES (lowercased owners). tr, not
        # ${VAR,,}: up.sh runs on the host, and macOS ships bash 3.2 where that
        # expansion is a syntax error.
        REPO_OWNER=$(printf '%s' "$REPO_OWNER" | tr '[:upper:]' '[:lower:]')
        IDENT=$(printf '%s' "$GIT_ORG_IDENTITIES" | awk -F'\t' -v o="$REPO_OWNER" '$1==o{print $2"\t"$3; exit}')
        ID_NAME="${IDENT%%$'\t'*}"; ID_EMAIL="${IDENT#*$'\t'}"
        if [ -n "$ID_NAME" ] || [ -n "$ID_EMAIL" ]; then
            docker exec -e "REPO_NAME=$RNAME" -e "ID_NAME=$ID_NAME" -e "ID_EMAIL=$ID_EMAIL" -u coder "$CNAME" bash -c '
                d="/workspace/repos/$REPO_NAME"; [ -d "$d/.git" ] || exit 0
                [ -n "$ID_NAME" ]  && git -C "$d" config user.name  "$ID_NAME"
                [ -n "$ID_EMAIL" ] && git -C "$d" config user.email "$ID_EMAIL"
                :' || true
        fi
    done <<EOF
$REPOS
EOF
else
    docker exec -u coder "$CNAME" bash -c \
        "[ -d /workspace/repos/scratch/.git ] || git init -b main /workspace/repos/scratch"
fi

# DEPRECATED(layout-v1 port): per-project agent state (session history, auto-
# memory) is keyed by the start-dir path — v1 keyed /workspace/main as
# -workspace-main; the v2 default start dir (/workspace/repos) keys as
# -workspace-repos. Copy once so a workspace reset keeps its memory (the auth
# volume the state lives in survives the reset). Remove this block once every
# container has been recreated on layout v2.
docker exec -u coder "$CNAME" bash -c \
    'src=/home/coder/.claude/projects/-workspace-main; dst=/home/coder/.claude/projects/-workspace-repos; if [ -d "$src" ] && [ ! -e "$dst" ]; then cp -a "$src" "$dst"; fi'

# Merge the manifest's repo list into dev.code-workspace (idempotent): a
# manifest edit on a live container adds its folder entry on the next up,
# while agent-managed worktree entries and hand-added folders survive.
REPO_NAMES="$(printf '%s' "$REPOS" | cut -f1 | tr '\n' ' ')"
docker exec -u coder -e REPO_NAMES="${REPO_NAMES:-scratch}" "$CNAME" \
    python3 /usr/local/lib/djinn/code_workspace.py /workspace/dev.code-workspace

# Workspace contract: a file no harness auto-loads (pi and codex walk ancestor
# directories for AGENTS.md, so that name would load it twice). compose_rules
# (below) appends it to the global rules file of every agent with a rules_file,
# so those harnesses read it through the same channel as the base rules. The
# old /workspace/CLAUDE.md copy is removed so Claude does not load the contract
# twice (this script generated it).
docker cp "$SCRIPT_DIR/docs/workspace.CONTRACT.md" "$CNAME:/workspace/CONTRACT.md"
docker exec "$CNAME" sh -c 'chown coder:coder /workspace/CONTRACT.md && rm -f /workspace/CLAUDE.md'

# ── Global rules fan-out (compose base rules + enabled-plugin fragments) ─────
# Each tool's global file is GENERATED (was a symlink to the read-only
# /agent-rules mount): the base rules plus the AGENTS.md fragment of every
# plugin THIS container enabled. The mount is :ro, so a fragment cannot be
# appended there — compose_rules.py reads it and writes real files into home.
# Output is byte-identical to the base until a plugin ships a fragment, and an
# interactive-shell hook (src/rules-compose.bashrc) recomposes so host-side
# edits to the base stay live. PLUGINS (space-separated enabled names) is the
# source of truth both composes read; skills stays a symlink (a dir, not text).
docker exec -u coder -e ENABLED_PLUGINS="$PLUGINS" "$CNAME" bash -c '
mkdir -p /home/coder/.claude /home/coder/.codex /home/coder/.config/djinn
printf "%s\n" "${ENABLED_PLUGINS:-}" > /home/coder/.config/djinn/enabled-plugins
python3 /usr/local/lib/djinn/compose_rules.py --announce
[ -e /home/coder/.claude/skills ] && [ ! -L /home/coder/.claude/skills ] || ln -sfn /agent-rules/skills /home/coder/.claude/skills
if [ ! -f /workspace/rules.local.md ]; then
cat > /workspace/rules.local.md <<EOF
# rules.local.md — container-local rule overrides

Rules that are global in spirit but specific to THIS project/container.
Not committed (lives outside the repo). Loaded by all agents alongside
/agent-rules/AGENTS.md. Precedence: repo rules > this file > global rules.
EOF
fi
echo "  ✓ skills linked (read-only; rules changes go via PR to the rules repo)"
'

# ── Wire agent MCP configs (one exec into the baked-in Python module) ────────
# All the config-file surgery — Claude's .mcp.json generation + ~/.claude.json
# pre-approval, the per-agent agent-scoped server rendering + plugin merges with
# sidecar stale-tracking, codex's managed TOML block — lives in
# src/wire_plugins.py (baked into the image, unit-tested by
# tests/test_wire_plugins.py). The SAME file also builds the JSON payload
# (--build-payload, host python3), so the descriptor-driven schema and runtime
# wiring rules live in one tested place; bash only routes env vars. Keys never
# enter the payload, and no longer travel alongside it either.
#
# NO SECRET VALUE CROSSES THIS BOUNDARY. There used to be an IDENTITY_KEY_n loop
# here: an agent that cannot hold a ${VAR} inside a remote MCP header needed the
# resolved key inlined into its config file, so up.sh handed the VALUE to the
# wiring exec on the `docker exec` argv — visible in `ps` on the host for the
# life of the call. Those agents take the mcp-remote shim now, which expands
# ${SLOT} from the agent's own process env, so the payload references nothing
# the exec has to carry. Keep it that way: a future change that needs a key here
# is reintroducing that leak.
PAYLOAD=$(AGENTS_MCP_JSON="$AGENTS_MCP_JSON" PLUGIN_MCP_ENTRIES="$PLUGIN_MCP_ENTRIES" \
    AGENT_SERVERS_JSON="$AGENT_SERVERS_JSON" AGENT_SECRETS="$AGENT_SECRETS" \
    "$PYTHON3" "$SCRIPT_DIR/src/wire_plugins.py" --build-payload)

printf '%s' "$PAYLOAD" | docker exec -i -u coder "$CNAME" \
    python3 /usr/local/lib/djinn/wire_plugins.py

# ── Container freshness stamps (PLN - Container Freshness Readout) ────────────
# Two host-truth timestamps the landing readout prints, so the human sees how
# old this container's config is and decides when to re-`up`. Written into
# /etc/environment (root-owned) AFTER the build/boot, because the image-built
# value only exists once the image is built. freshness.py reads them there for
# both attach (`docker exec`, no PAM) and SSH shells.
#   last `up`    now, every run — external rules (pulled each `up`) + MCP wiring
#   image built  the image's real .Created via the container's image ID; a full
#                cache hit leaves it old, which is the honest signal for the
#                baked half (bundled rules, plugin fragments, install: blocks).
UP_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
IMAGE_ID="$(docker inspect -f '{{.Image}}' "$CNAME" 2>/dev/null || true)"
IMAGE_BUILT="$(docker inspect -f '{{.Created}}' "$IMAGE_ID" 2>/dev/null || true)"
# Non-fatal (|| true): the readout is cosmetic — a failure to stamp must never
# abort an otherwise-successful `up` (zero runtime failure surface, by design).
docker exec "$CNAME" bash -c '
    sed -i "/^DJINN_UP_AT=/d;/^DJINN_IMAGE_BUILT=/d" /etc/environment
    printf "DJINN_UP_AT=%s\nDJINN_IMAGE_BUILT=%s\n" "$1" "$2" >> /etc/environment
' _ "$UP_AT" "$IMAGE_BUILT" || true

# ── Plugin setup (setup: in plugin.yml — PLN "plugin setup hook" §[2/3]) ──────
# One `docker exec` PER plugin with a setup command, after the enabled-plugins
# file and MCP wiring above (a setup step may wire into what those write) and
# BEFORE the services section below (a service may depend on what setup
# wired). src/plugin_setup.py renders one flat script per (plugin, command)
# pair and up.sh pipes it in on stdin (never `bash -c "<script>"` — see that
# module's docstring for why); PLUGIN_SETUP already carries only ENABLED
# plugins, one command each, cross-plugin uniqueness is the plugin name
# itself. The command re-runs on every `./djinn up` and must be idempotent
# (docs say so); it exits with the command's exit code and a failure here is
# loud but non-fatal to `up` — the same posture as a failed service start.
if [ -n "$PLUGIN_SETUP" ]; then
    echo "  Plugin setup:"
    while IFS=$'\t' read -r SETUP_PLUGIN SETUP_CMD; do
        [ -n "$SETUP_PLUGIN" ] || continue
        # Generate first, exec second: a pipeline would take docker exec's
        # exit status and mask a generator failure (no pipefail in up.sh).
        if SETUP_SCRIPT=$("$PYTHON3" "$SCRIPT_DIR/src/plugin_setup.py" "$SETUP_PLUGIN" "$SETUP_CMD"); then
            printf '%s\n' "$SETUP_SCRIPT" | docker exec -i -u coder "$CNAME" bash \
                || echo "  ! setup $SETUP_PLUGIN failed — see above"
        else
            echo "  ! setup $SETUP_PLUGIN: script generation failed — see above"
        fi
    done <<EOF
$PLUGIN_SETUP
EOF
fi

# ── Plugin services (services: in plugin.yml — Phase 1 Hardening PLN §2) ──────
# One `docker exec` PER service, after everything above so the container,
# repos, and MCP wiring are already in place. src/plugin_services.py renders
# an idempotent tmux-guard + logging restart wrapper for one (name, command)
# pair and up.sh pipes it in on stdin (never `bash -c "<script>"` — see that
# module's docstring for why); PLUGIN_SERVICES already carries only the
# services of ENABLED plugins, cross-plugin name collisions already rejected
# by src/manifest.py. `tmux has-session` inside the generated script makes a
# repeat `./djinn up` restart only whatever died since the last run.
if [ -n "$PLUGIN_SERVICES" ]; then
    echo "  Plugin services:"
    while IFS=$'\t' read -r SVC_NAME SVC_CMD SVC_PLUGIN; do
        [ -n "$SVC_NAME" ] || continue
        # Generate first, exec second: a pipeline would take docker exec's
        # exit status and mask a generator failure (no pipefail in up.sh).
        if SVC_SCRIPT=$("$PYTHON3" "$SCRIPT_DIR/src/plugin_services.py" "$SVC_NAME" "$SVC_CMD"); then
            printf '%s\n' "$SVC_SCRIPT" | docker exec -i -u coder "$CNAME" bash \
                || echo "  ! svc-$SVC_NAME (plugin '$SVC_PLUGIN') failed to start — see above"
        else
            echo "  ! svc-$SVC_NAME (plugin '$SVC_PLUGIN'): wrapper generation failed — see above"
        fi
    done <<EOF
$PLUGIN_SERVICES
EOF
fi

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  $CNAME is up (manifest: $MANIFEST)"
echo ""
echo "  VS Code / Cursor:  Dev Containers: Attach to Running Container"
echo "  Terminal:          docker exec -it -u coder $CNAME bash"
echo "  Claude:            cd /workspace/repos && claude   (one session over every repo)"
[ -n "$SSH_PORT" ] && echo "  SSH:               ssh -p $SSH_PORT coder@$( [ "$SSH_BIND" = "127.0.0.1" ] && echo localhost || echo '<this-host>' )"
if [ "$REMOTE_JUMP" = "true" ] && [ -n "$JUMP_IP" ] && [ -n "${JUMP_AUTHORIZED_KEY:-}" ]; then
    echo "  Jump:              mosh coder@$JUMP_IP  then  ssh djinn-$NAME"
fi
if [ -n "$SSH_PORT" ]; then
    # Direct bridge access only works on the published path: the firewall
    # otherwise accepts :22 from the jump alone, never the whole bridge/tunnel.
    TUNNEL_IP="$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$CNAME" 2>/dev/null || true)"
    echo "  Remote (tunnel):   ${TUNNEL_IP:-<no ip>} — ssh coder@ip over your WireGuard/VPN"
fi
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
