#!/bin/bash
# up.sh <name> — declaratively create or update an agent dev container from
# containers/<name>.yml. Idempotent: edit the manifest, rerun, done.
#
# Kept:     the manifest (containers/*.yml) and ~/dev-agent/secrets.env
# Derived:  ~/dev-agent/keys/<name>/ (recomposed every run), the container,
#           generated .mcp.json / dev.code-workspace / workspace CLAUDE.md
# Survives: workspace volume (code), ~/dev-agent/artifacts/<name>/
#
# Requires: docker, yq (brew install yq / static binary on Linux), python3
# (stdlib only — owns ALL manifest validation/derivation via src/manifest.py
# and builds the wiring payload; yq only converts YAML→JSON. The wiring
# itself runs in-container via the baked-in src/wire_plugins.py).

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
. "$SCRIPT_DIR/src/common.sh"   # sources ./.env, sets BASE_PATH (the dev-agent home)

NAME="$1"
if [ -z "$NAME" ]; then
    echo "Usage: ./up.sh <name>    (reads $CONTAINERS_PATH/<name>.yml)"
    echo "Manifests:"
    for f in "$CONTAINERS_PATH"/*.yml; do
        [ -f "$f" ] || continue
        n=$(basename "$f" .yml)
        [ "$n" = "TEMPLATE" ] && continue
        printf "  %s\n" "$n"
    done
    exit 1
fi

# CONTAINERS_PATH (resolved in common.sh) is where manifests live: the repo's
# containers/ by default, or $BASE_PATH/containers / a CONTAINERS_PATH override
# when you keep them in a private repo outside this one.
MANIFEST="$CONTAINERS_PATH/$NAME.yml"
[ -f "$MANIFEST" ] || { echo "Error: no manifest at $MANIFEST (cp $SCRIPT_DIR/containers/TEMPLATE.yml $MANIFEST)"; exit 1; }
command -v yq >/dev/null || { echo "Error: yq required (brew install yq)"; exit 1; }
# Host python3 (stdlib-only, builds the wiring payload): prefer the SYSTEM
# interpreter over whatever shim leads $PATH — pyenv/homebrew pythons can be
# present-but-broken (dyld: library not loaded) in ways `command -v` cannot
# see, so each candidate must actually RUN. Override with PYTHON3=/path.
if [ -z "${PYTHON3:-}" ]; then
    for cand in /usr/bin/python3 python3; do
        if "$cand" -c '' 2>/dev/null; then PYTHON3="$cand"; break; fi
    done
fi
[ -n "${PYTHON3:-}" ] && "$PYTHON3" -c '' 2>/dev/null \
    || { echo "Error: no working python3 (tried /usr/bin/python3 and PATH — broken pyenv/brew shim? Install Xcode CLT on macOS, or set PYTHON3=/path/to/python3)"; exit 1; }

mkdir -p "$BASE_PATH"   # create the dev-agent home now that we're proceeding
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
PRESENT_SECRET_VARS=""
for v in $(compgen -v); do
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
    } | PRESENT_SECRET_VARS="$PRESENT_SECRET_VARS" GH_TOKEN_VARS="$GH_TOKEN_VARS" \
        SECRETS_FILE="$SECRETS_FILE" \
        GIT_NAME_DEFAULT="$(git config --global user.name 2>/dev/null || true)" \
        GIT_EMAIL_DEFAULT="$(git config --global user.email 2>/dev/null || true)" \
        NTFY_URL="${NTFY_URL:-}" NTFY_TOPIC="${NTFY_TOPIC:-}" \
        "$PYTHON3" "$SCRIPT_DIR/src/manifest.py" --derive
)
eval "$DERIVED"

# Resolve the container's default git token from the manifest's git.token (a
# secrets.env var NAME; manifest.py already checked it is set). Absent → keep
# GH_TOKEN as sourced from secrets.env, so manifests with no git.token keep the
# global machine-user token (backward compatible). This GH_TOKEN is what
# keyfiles.sh fans into every <agent>.env and the clone bootstrap hands to git.
if [ -n "$GIT_TOKEN_SOURCE" ]; then GH_TOKEN="${!GIT_TOKEN_SOURCE}"; fi

COMPOSE_FILES="-f $SCRIPT_DIR/compose/docker-compose.local.yml"
[ -n "$SSH_PORT" ] && COMPOSE_FILES="$COMPOSE_FILES -f $SCRIPT_DIR/compose/docker-compose.ssh.yml"
[ "$REMOTE_MOSH" = "true" ] && COMPOSE_FILES="$COMPOSE_FILES -f $SCRIPT_DIR/compose/docker-compose.mosh.yml"

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
SHIM_AGENTS="claude pi gemini cursor-agent codex"
. "$SCRIPT_DIR/src/keyfiles.sh"
write_keyfiles "$KEYS_PATH" "$SHIM_AGENTS" "$PLUGIN_ENV_SECRETS" "$AGENT_SECRETS" "$GIT_ORG_TOKENS"

# ── Host paths + platform ─────────────────────────────────────────────────────
ARTIFACTS_PATH="$BASE_PATH/artifacts/$NAME"
mkdir -p "$ARTIFACTS_PATH"
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
# the bundled copy — it lives inside THIS repo, so a pull would pull docker-dev.
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
# One user-defined bridge with a stable subnet (override via DEV_AGENT_SUBNET
# in ./.env). Existing containers adopt it on their next recreate.
DESIRED_SUBNET="${DEV_AGENT_SUBNET:-172.30.0.0/24}"
if docker network inspect dev-agent-net >/dev/null 2>&1; then
    # The subnet is fixed at creation — warn loudly if the override drifted,
    # or the operator points their VPN route at a CIDR no container is on.
    ACTUAL_SUBNET=$(docker network inspect -f '{{(index .IPAM.Config 0).Subnet}}' dev-agent-net 2>/dev/null || true)
    if [ -n "$ACTUAL_SUBNET" ] && [ "$ACTUAL_SUBNET" != "$DESIRED_SUBNET" ]; then
        echo "  ⚠ dev-agent-net already exists with subnet $ACTUAL_SUBNET (config wants $DESIRED_SUBNET)."
        echo "    To change it: stop all dev-agent containers, 'docker network rm dev-agent-net', rerun up.sh."
    fi
else
    echo "Creating shared network dev-agent-net ($DESIRED_SUBNET)"
    # `|| inspect` tolerates losing a create race to a concurrent up.sh run.
    if ! docker network create --subnet "$DESIRED_SUBNET" dev-agent-net >/dev/null 2>&1 \
        && ! docker network inspect dev-agent-net >/dev/null 2>&1; then
        echo "Error: could not create dev-agent-net ($DESIRED_SUBNET) — the subnet may overlap an existing docker network."
        echo "Pick a free range via DEV_AGENT_SUBNET in ./.env (docker auto-allocates inside 172.17-172.31)."
        exit 1
    fi
fi

# ── Apply ─────────────────────────────────────────────────────────────────────
echo "Applying $MANIFEST → dev-agent-$NAME"
REMOTE_SUMMARY=""
[ "$REMOTE_TMUX" = "true" ] && REMOTE_SUMMARY="tmux"
[ "$REMOTE_MOSH" = "true" ] && REMOTE_SUMMARY="${REMOTE_SUMMARY:+$REMOTE_SUMMARY+}mosh"
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
INSTALL_CLAUDE="$INSTALL_CLAUDE" INSTALL_CODEX="$INSTALL_CODEX" \
INSTALL_PI="$INSTALL_PI" INSTALL_GEMINI="$INSTALL_GEMINI" \
INSTALL_CURSOR="$INSTALL_CURSOR" INSTALL_AIDER="$INSTALL_AIDER" \
HOST_MCP_PORTS="$HOST_MCP_PORTS" EXTRA_ALLOWED_DOMAINS="$EGRESS" \
ALLOWED_CIDRS="$EGRESS_CIDRS" \
KEYS_PATH="$KEYS_PATH" ARTIFACTS_PATH="$ARTIFACTS_PATH" MEM_LIMIT="$MEM_LIMIT" \
SSH_PORT="$SSH_PORT" SSH_BIND="$SSH_BIND" SSH_AUTHORIZED_KEY="${SSH_AUTHORIZED_KEY:-}" \
REMOTE_TMUX="$REMOTE_TMUX" \
MOSH_PORTS="$MOSH_PORTS" MOSH_PORTS_DASH="$MOSH_PORTS_DASH" \
NTFY_URL="$CONTAINER_NTFY_URL" NTFY_TOPIC="$CONTAINER_NTFY_TOPIC" \
IMAGE_TAG="$NAME" \
docker compose -p "dev-agent-$NAME" --project-directory "$SCRIPT_DIR" \
    $COMPOSE_FILES up -d --build

# ── Wait for entrypoint/firewall ──────────────────────────────────────────────
# Crash-loop detection compares against the restart count captured now (0 for
# a freshly (re)created container; the current value for a healthy no-op
# re-up). A rise DURING the wait = a crash loop this run — which also catches
# the SSH-missing-key case where 'firewall active' prints before the fatal
# exit (the marker alone would falsely read as success).
BASELINE_RESTARTS="$(docker inspect -f '{{.RestartCount}}' "dev-agent-$NAME" 2>/dev/null || echo 0)"
i=0
READY=false
while [ $i -lt 24 ]; do
    STATUS="$(docker inspect -f '{{.State.Status}}' "dev-agent-$NAME" 2>/dev/null || echo missing)"
    if [ "$STATUS" = "exited" ] || [ "$STATUS" = "missing" ] || [ "$STATUS" = "restarting" ]; then
        echo "Error: container failed to start. Logs:"
        docker logs "dev-agent-$NAME" 2>&1 | tail -20
        exit 1
    fi
    RESTART_COUNT="$(docker inspect -f '{{.RestartCount}}' "dev-agent-$NAME" 2>/dev/null || echo 0)"
    if [ "$RESTART_COUNT" -gt "$BASELINE_RESTARTS" ]; then
        echo "Error: container crash-loop detected (restarts rose to $RESTART_COUNT). Logs:"
        docker logs "dev-agent-$NAME" 2>&1 | tail -20
        exit 1
    fi
    if docker logs "dev-agent-$NAME" 2>&1 | grep -q "firewall active\|firewall DISABLED"; then
        # The marker persists in logs across restarts, so a crashing boot can
        # print it too. Confirm the container is actually STABLE: still running
        # and no new restart 2s later. A crash loop keeps incrementing, so this
        # catches a container that logged the marker then died.
        sleep 2
        CONFIRM_STATUS="$(docker inspect -f '{{.State.Status}}' "dev-agent-$NAME" 2>/dev/null || echo missing)"
        CONFIRM_RESTARTS="$(docker inspect -f '{{.RestartCount}}' "dev-agent-$NAME" 2>/dev/null || echo 0)"
        if [ "$CONFIRM_STATUS" != "running" ] || [ "$CONFIRM_RESTARTS" -gt "$BASELINE_RESTARTS" ]; then
            echo "Error: container crash-loop detected (unstable after readiness marker). Logs:"
            docker logs "dev-agent-$NAME" 2>&1 | tail -20
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
    docker logs "dev-agent-$NAME" 2>&1 | tail -20
    exit 1
fi

# ── Bootstrap workspace (idempotent; layout v2: /workspace/repos/<name>) ─────
# A v1 workspace (/workspace/main) cannot migrate in place — worktree metadata
# pins absolute paths — so refuse loudly instead of half-operating on a mixed
# layout. An EMPTY main/ (v1 entrypoint pre-create, never bootstrapped) is
# harmless: rmdir it and proceed. The refusal recommends the selective volume
# reset, NOT --purge: purge would also delete the auth volumes (agent logins,
# per-project state) that the reset — and the port shim below — preserve.
docker exec -u coder "dev-agent-$NAME" bash -c 'rmdir /workspace/main 2>/dev/null || true'
if docker exec -u coder "dev-agent-$NAME" bash -c '[ -e /workspace/main ]'; then
    echo "Error: this workspace uses layout v1 (/workspace/main). Layout v2 puts every repo under /workspace/repos/<name>."
    echo "Push every branch you care about, then reset the workspace volume (agent auth/state survives) and rerun:"
    echo "  ./down.sh $NAME && docker volume rm dev-agent-${NAME}_workspace && ./up.sh $NAME"
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
        docker exec $CLONE_ENV -e "REPO_NAME=$RNAME" -e "REPO_URL=$RURL" -u coder "dev-agent-$NAME" bash -c \
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
            docker exec -e "REPO_NAME=$RNAME" -e "ID_NAME=$ID_NAME" -e "ID_EMAIL=$ID_EMAIL" -u coder "dev-agent-$NAME" bash -c '
                d="/workspace/repos/$REPO_NAME"; [ -d "$d/.git" ] || exit 0
                [ -n "$ID_NAME" ]  && git -C "$d" config user.name  "$ID_NAME"
                [ -n "$ID_EMAIL" ] && git -C "$d" config user.email "$ID_EMAIL"
                :' || true
        fi
    done <<EOF
$REPOS
EOF
else
    docker exec -u coder "dev-agent-$NAME" bash -c \
        "[ -d /workspace/repos/scratch/.git ] || git init -b main /workspace/repos/scratch"
fi

# DEPRECATED(layout-v1 port): per-project agent state (session history, auto-
# memory) is keyed by the start-dir path — v1 keyed /workspace/main as
# -workspace-main; the v2 default start dir (/workspace/repos) keys as
# -workspace-repos. Copy once so a workspace reset keeps its memory (the auth
# volume the state lives in survives the reset). Remove this block once every
# container has been recreated on layout v2.
docker exec -u coder "dev-agent-$NAME" bash -c \
    'src=/home/coder/.claude/projects/-workspace-main; dst=/home/coder/.claude/projects/-workspace-repos; if [ -d "$src" ] && [ ! -e "$dst" ]; then cp -a "$src" "$dst"; fi'

# Merge the manifest's repo list into dev.code-workspace (idempotent): a
# manifest edit on a live container adds its folder entry on the next up,
# while agent-managed worktree entries and hand-added folders survive.
REPO_NAMES="$(printf '%s' "$REPOS" | cut -f1 | tr '\n' ' ')"
docker exec -u coder -e REPO_NAMES="${REPO_NAMES:-scratch}" "dev-agent-$NAME" \
    python3 /usr/local/lib/dev-agent/code_workspace.py /workspace/dev.code-workspace

docker cp "$SCRIPT_DIR/docs/workspace.CLAUDE.md" "dev-agent-$NAME:/workspace/CLAUDE.md"
docker exec "dev-agent-$NAME" chown coder:coder /workspace/CLAUDE.md

# ── Global rules fan-out (compose base rules + enabled-plugin fragments) ─────
# Each tool's global file is GENERATED (was a symlink to the read-only
# /agent-rules mount): the base rules plus the AGENTS.md fragment of every
# plugin THIS container enabled. The mount is :ro, so a fragment cannot be
# appended there — compose_rules.py reads it and writes real files into home.
# Output is byte-identical to the base until a plugin ships a fragment, and an
# interactive-shell hook (src/rules-compose.bashrc) recomposes so host-side
# edits to the base stay live. PLUGINS (space-separated enabled names) is the
# source of truth both composes read; skills stays a symlink (a dir, not text).
docker exec -u coder -e ENABLED_PLUGINS="$PLUGINS" "dev-agent-$NAME" bash -c '
mkdir -p /home/coder/.claude /home/coder/.codex /home/coder/.gemini /home/coder/.config/dev-agent
printf "%s\n" "${ENABLED_PLUGINS:-}" > /home/coder/.config/dev-agent/enabled-plugins
python3 /usr/local/lib/dev-agent/compose_rules.py --announce
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
# (--build-payload, host python3), so the schema and strict boolean semantics
# live in one tested place; bash only routes env vars. Keys never enter the
# payload: they travel as docker-exec env vars the payload references by name —
# and only for cursor-agent/gemini/pi (claude expands the ${VAR} ref from its
# shim env; codex is a pending warning and ships no key at all).
#
# Build literal remote-agent inputs from resolved AGENT_SECRETS. Only slots
# required by an MCP server travel through docker exec; env-only slots are
# already in the per-agent key file. Claude keeps ${SLOT} references and Codex
# still warns for remote MCPs, so neither receives a literal key here.
IDENTITY_ENV=()
IDENTITY_SECRETS=""
i=0
while IFS=$'\t' read -r agent slot source; do
    [ -n "$agent" ] || continue
    case " $AGENT_SERVER_SLOTS " in *" $slot "*) ;; *) continue ;; esac
    case "$agent" in
        claude|codex) ;;
        *)
            IDENTITY_ENV+=(-e "IDENTITY_KEY_${i}=${!source}")
            IDENTITY_SECRETS="${IDENTITY_SECRETS:+$IDENTITY_SECRETS }$agent:IDENTITY_KEY_$i:$slot"
            i=$((i + 1)) ;;
    esac
done <<EOF
$AGENT_SECRETS
EOF

PAYLOAD=$(WIRE_CURSOR="$INSTALL_CURSOR" WIRE_GEMINI="$INSTALL_GEMINI" \
    WIRE_PI="$INSTALL_PI" WIRE_CODEX="$INSTALL_CODEX" \
    PLUGIN_MCP_ENTRIES="$PLUGIN_MCP_ENTRIES" \
    AGENT_SERVERS_JSON="$AGENT_SERVERS_JSON" AGENT_SECRETS="$AGENT_SECRETS" \
    IDENTITY_SECRETS="$IDENTITY_SECRETS" \
    "$PYTHON3" "$SCRIPT_DIR/src/wire_plugins.py" --build-payload)

printf '%s' "$PAYLOAD" | docker exec -i -u coder "${IDENTITY_ENV[@]}" "dev-agent-$NAME" \
    python3 /usr/local/lib/dev-agent/wire_plugins.py

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
IMAGE_ID="$(docker inspect -f '{{.Image}}' "dev-agent-$NAME" 2>/dev/null || true)"
IMAGE_BUILT="$(docker inspect -f '{{.Created}}' "$IMAGE_ID" 2>/dev/null || true)"
# Non-fatal (|| true): the readout is cosmetic — a failure to stamp must never
# abort an otherwise-successful `up` (zero runtime failure surface, by design).
docker exec "dev-agent-$NAME" bash -c '
    sed -i "/^DEV_AGENT_UP_AT=/d;/^DEV_AGENT_IMAGE_BUILT=/d" /etc/environment
    printf "DEV_AGENT_UP_AT=%s\nDEV_AGENT_IMAGE_BUILT=%s\n" "$1" "$2" >> /etc/environment
' _ "$UP_AT" "$IMAGE_BUILT" || true

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  dev-agent-$NAME is up (manifest: $MANIFEST)"
echo ""
echo "  VS Code / Cursor:  Dev Containers: Attach to Running Container"
echo "  Terminal:          docker exec -it -u coder dev-agent-$NAME bash"
echo "  Claude:            cd /workspace/repos && claude   (one session over every repo)"
[ -n "$SSH_PORT" ] && echo "  SSH:               ssh -p $SSH_PORT coder@$( [ "$SSH_BIND" = "127.0.0.1" ] && echo localhost || echo '<this-host>' )"
if [ "$REMOTE_TMUX" = "true" ] || [ "$REMOTE_MOSH" = "true" ]; then
    TUNNEL_IP="$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "dev-agent-$NAME" 2>/dev/null || true)"
    echo "  Remote (tunnel):   ${TUNNEL_IP:-<no ip>} — $( [ "$REMOTE_MOSH" = "true" ] && echo "mosh coder@ip (UDP $MOSH_PORTS_DASH)" || echo "ssh coder@ip" ) over your WireGuard/VPN"
fi
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
