#!/bin/bash
set -e

CONTAINER_NAME="${CONTAINER_NAME:-unnamed}"

echo "╔══════════════════════════════════════════╗"
echo "║        Agent Dev Container         ║"
echo "║        ${CONTAINER_NAME}                 ║"
echo "╚══════════════════════════════════════════╝"

# ── Persist ~/.claude.json via symlink ────────────────────────────────────────
# Non-fatal (|| true): a failure here (e.g. an unexpectedly root-owned volume
# mountpoint) must not crash-loop the container before the firewall runs — it
# only means claude.json isn't persisted this boot.
su coder -c 'if [ ! -L /home/coder/.claude.json ]; then [ -f /home/coder/.claude.json ] && mv /home/coder/.claude.json /home/coder/.claude/claude.json; ln -sf /home/coder/.claude/claude.json /home/coder/.claude.json; fi' || true

# ── Plugin-declared volume mountpoints ───────────────────────────────────────
# PLUGIN_VOLUME_PATHS arrives from the generated compose overlay (one entry per
# volume: in an enabled plugin.yml). Docker seeds a fresh named volume from the
# image directory it covers, ownership included — so a mountpoint the image does
# not contain arrives root-owned and the coder-run agent silently cannot write
# to it (exactly how cursor-agent's auth failed to persist). We run as root
# here, before any agent, so fix it centrally rather than trusting every plugin
# to mkdir its own path at build.
#
# The loop is unquoted to word-split the space-separated list, which also turns
# on globbing: `set -f` off/on around it so a path containing '*' can never
# chown a DIFFERENT directory than the one that was mounted (manifest.py also
# rejects glob characters — this is the second lock on the same door).
#
# Docker creates every missing PARENT of the mountpoint root-owned too, so walk
# up from the mountpoint chowning root-owned ancestors and stop at the first one
# that is already coder's. Without it a path one level below an existing image
# directory leaves its parent unwritable — the plugin can write its own file but
# not a sibling next to it. Bounded twice: manifest.py confines these paths to
# /home/coder/, and the walk halts as soon as ownership is already right, so it
# can never climb past the coder-owned home. Each chown is non-recursive: only
# freshly created directories can be wrong, and they are empty by definition —
# a recursive chown would walk a cache of hundreds of MB on every boot.
set -f
for _vol_path in ${PLUGIN_VOLUME_PATHS:-}; do
    if mkdir -p "$_vol_path"; then
        _p="$_vol_path"
        while [ "$_p" != "/" ] && [ "$(stat -c %u "$_p" 2>/dev/null || echo 1)" = 0 ]; do
            chown coder:coder "$_p" || break
            _p="$(dirname "$_p")"
        done
        echo "✓ Plugin volume: $_vol_path"
    else
        echo "⚠ Plugin volume $_vol_path: could not create mountpoint"
    fi
done
set +f
unset _vol_path _p

# ── Host gateway resolves over IPv4 only ─────────────────────────────────────
# Docker Desktop writes host.docker.internal twice, A and AAAA. The container
# has no route to that IPv6 ULA, but getaddrinfo() sorts it first, so every
# in-container client that resolves the name by hand (request-egress and the
# NFLOG reader filing with the broker) failed with "Network is unreachable"
# while the broker sat reachable on the v4 address. Non-fatal: without it
# resolution is slow-and-broken for those clients, not the whole container.
python3 /usr/local/lib/djinn/hosts_ipv4.py || true

# ── Egress firewall (default ON — fail loud, never run open) ─────────────────
if [ "${ENABLE_FIREWALL:-true}" = "true" ]; then
    if /usr/local/bin/init-firewall.sh; then
        echo "✓ Egress firewall active"
        if [ "${ENABLE_EGRESS_BROKER:-true}" = "true" ]; then
            if PYTHONPATH=/usr/local/lib/djinn python3 /usr/local/lib/djinn/egress_broker.py --supervise & then
                echo "✓ Egress transparent broker active"
            else
                /usr/local/bin/egress_broker_firewall.sh remove || true
                echo ""
                echo "FATAL: egress transparent broker failed to start."
                echo "Removed broker REDIRECT rules to avoid blackholing :80/:443."
                exit 1
            fi
            if PYTHONPATH=/usr/local/lib/djinn python3 /usr/local/lib/djinn/egress_nflog.py & then
                echo "✓ Egress NFLOG reader started"
            else
                echo "⚠ Egress NFLOG reader failed to start (non-fatal)"
            fi
        fi
    else
        echo ""
        echo "FATAL: firewall setup failed (missing NET_ADMIN/NET_RAW caps?)."
        echo "Refusing to start with open egress. Set ENABLE_FIREWALL=false to"
        echo "run without the firewall, or add cap_add: [NET_ADMIN, NET_RAW]."
        exit 1
    fi
else
    echo "⚠ Egress firewall DISABLED (ENABLE_FIREWALL=false)"
fi

# ── Git config ────────────────────────────────────────────────────────────────
if [ -n "$GIT_USER_NAME" ]; then
    GIT_USER_NAME="$GIT_USER_NAME" su coder -c 'git config --global user.name "$GIT_USER_NAME"'
    echo "✓ Git user.name: $GIT_USER_NAME"
fi
if [ -n "$GIT_USER_EMAIL" ]; then
    GIT_USER_EMAIL="$GIT_USER_EMAIL" su coder -c 'git config --global user.email "$GIT_USER_EMAIL"'
    echo "✓ Git user.email: $GIT_USER_EMAIL"
fi

# (No /workspace/.mcp.json symlink: Claude Code reads .mcp.json only from
# its start directory — the canonical per-container config lives at
# /workspace/repos/.mcp.json and is symlinked into each repo by wire_plugins.py
# and into each worktree per the workspace contract.)

# ── Workspace skeleton (always present so editors can attach) ────────────────
# Layout v2: every repo lives under /workspace/repos/<name>. Guarantee the
# container-owned anchor dirs exist at EVERY boot — independent of whether
# up.sh's clone bootstrap has run yet, and surviving a failed private-repo
# clone — so "Attach to Running Container" never dies on a missing cwd. The
# repo dirs themselves appear only when their clone succeeds; up.sh's
# per-repo `[ -d …/.git ]` guard retries failed clones on a later rerun.
su coder -c 'mkdir -p /workspace/repos /workspace/worktrees'

# ── Git safe directory ────────────────────────────────────────────────────────
su -c "git config --global safe.directory /workspace" coder

# ── Git over HTTPS via the per-org credential router ─────────────────────────
# One credential lane for both API and git transport, routed by repo OWNER:
# git-credential-org returns GH_TOKEN_<owner> if set (per-org identity), else
# the container GH_TOKEN, else defers to `gh auth git-credential` for the human
# login — so agents present the right per-org token and humans still fall back
# to the shared gh login. No SSH keys. useHttpPath=true feeds the repo path to
# the router so it can read the owner (and makes credential caching per-path,
# which is harmless here). Router is github-only for now; gitea is a follow-up.
su -c "git config --global credential.useHttpPath true" coder
# VS Code's dev-container GitHub feature pre-seeds credential.'https://github.com'.helper
# (= !gh auth git-credential) on every attach, and can duplicate it across windows/
# re-attaches. A plain `git config` set then aborts with "cannot overwrite multiple
# values", leaving the router UNinstalled — and the desktop credential bridge
# (credential.helper in /etc/gitconfig) answers first, so git ops leak the human's
# login instead of the per-org token. Reset the github.com helper list (empty value)
# and add the router as the leading helper: idempotent across re-runs and
# authoritative over the desktop bridge.
su -c "git config --global --unset-all credential.'https://github.com'.helper" coder 2>/dev/null || true
su -c "git config --global --add credential.'https://github.com'.helper ''" coder
su -c "git config --global --add credential.'https://github.com'.helper /usr/local/bin/git-credential-org" coder

# ── SSH mode vs attach mode ───────────────────────────────────────────────────
# remote_access.py owns the START_SSHD decision — two independent paths can
# want sshd: explicit ssh: (host-published, fails loud with no key) and the
# default jump path (bridge-only, degrades quietly — a bottle with no key
# yet just isn't jump-reachable, never a crash loop) — and rebuilds
# authorized_keys to match (see src/remote_access.py; AGENTS.md "Python over
# bash"). Captured into a variable BEFORE eval, not `eval "$(…)"` inline: a
# failed command substitution's exit status is lost once `eval` runs on its
# (empty, on failure) output, so `set -e` would silently continue past the
# published-no-key FATAL case otherwise. Same two-step shape as up.sh's own
# `DERIVED=$(...); eval "$DERIVED"`.
SSHD_ARGS=$(python3 /usr/local/lib/djinn/remote_access.py sshd \
    --authorized-keys /home/coder/.ssh/authorized_keys)
eval "$SSHD_ARGS"

# ── herdr notify watcher (PLN - herdr adoption P2) ───────────────────────────
# Event-driven remote.notify for the herdr landing: pushes to ntfy on agent
# state transitions while nobody is attached. Independent of sshd — attach-
# mode bottles land VS Code terminals in herdr too — so it starts before the
# sshd/attach fork below (both branches exec and never return). Non-login su
# keeps the container env (NTFY_URL, NTFY_TOPIC, CONTAINER_NAME) and sets
# HOME; nothing is interpolated into the command string, so a quote in a
# secrets.env value cannot become shell. Its stdout is docker logs.
if [ "${REMOTE_SHELL:-herdr}" = "herdr" ] && [ -n "${NTFY_URL:-}" ]; then
    su coder -c 'exec python3 /usr/local/lib/djinn/herdr_notify.py' &
    echo "✓ herdr notify watcher started (ntfy, pid $!)"
fi

if [ "$START_SSHD" = "true" ]; then
    chown coder:coder /home/coder/.ssh/authorized_keys

    # Host keys live on the ssh-host-keys volume (compose), not in the image
    # layer: a regenerated key is a REMOTE HOST IDENTIFICATION HAS CHANGED
    # refusal on the jump's next hop after every rebuild. sshd_config's
    # HostKey lines (Dockerfile) point here; same loop as jump/entrypoint.sh.
    host_keys_generated=0
    for type in rsa ecdsa ed25519; do
        key="/etc/ssh/host_keys/ssh_host_${type}_key"
        if [ ! -f "$key" ]; then
            ssh-keygen -q -t "$type" -N "" -f "$key" </dev/null
            host_keys_generated=$((host_keys_generated + 1))
        fi
        chmod 600 "$key"
    done
    echo "✓ SSH host keys ready (generated=$host_keys_generated, persisted on the ssh-host-keys volume)"

    # RFC 04: sshd builds each session's env via PAM (/etc/environment) and
    # ignores container env — persist the remote-access vars there so login
    # shells (and the tmux server/hooks they start) can see them.
    for var in REMOTE_SHELL NTFY_URL NTFY_TOPIC CONTAINER_NAME; do
        val="${!var:-}"
        [ -n "$val" ] || continue
        sed -i "/^$var=/d" /etc/environment
        echo "$var=$val" >> /etc/environment
    done
    case "${REMOTE_SHELL:-herdr}" in
        tmux)  echo "✓ Remote access: logins land in a fresh tmux session (picker when others exist)" ;;
        herdr) echo "✓ Remote access: logins land in the bottle's herdr workspace (detach ctrl+b q)" ;;
    esac

    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    if [ "$SSHD_MODE" = "published" ]; then
        echo "  Container:  djinn-${CONTAINER_NAME}   (sshd on :22, published"
        echo "              on the host at the manifest's ssh.port)"
        echo "  SSH:        ssh -p <ssh.port> coder@<docker-host>"
        echo "  VS Code:    Remote-SSH to the same host/port"
    else
        # Default jump path: no host port at all — the firewall accepts :22
        # ONLY from the jump's static bridge IP (init-firewall.sh), so a
        # direct ssh/mosh to this bottle's bridge address is refused by
        # design. DJINN_JUMP_IP arrives via compose (up.sh resolves it
        # host-side); empty until a jump address has ever been derived.
        echo "  Container:  djinn-${CONTAINER_NAME}   (sshd on the bridge :22,"
        echo "              reachable ONLY from the jump — no host port)"
        echo "  Jump:       mosh coder@${DJINN_JUMP_IP:-<jump ip>}  then  ssh djinn-${CONTAINER_NAME}"
    fi
    echo "  Workspace:  /workspace"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""

    echo "Starting sshd..."
    exec /usr/sbin/sshd -D
else
    # Not starting sshd: remote_access.py already removed a stale
    # authorized_keys file left by a previous boot (or a manifest edit that
    # turned SSH off) so a later `docker exec` root shell can't find
    # leftover key material implying reachability that no longer applies.
    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "  Container:  djinn-${CONTAINER_NAME}   (attach mode, no sshd)"
    echo ""
    echo "  VS Code / Cursor:"
    echo "    Dev Containers: Attach to Running Container → djinn-${CONTAINER_NAME}"
    echo "    then open /workspace (or /workspace/dev.code-workspace)"
    echo ""
    echo "  Terminal:   docker exec -it -u coder djinn-${CONTAINER_NAME} bash"
    echo "  Workspace:  /workspace"
    echo ""
    echo "  Dev servers: use VS Code port forwarding, or publish ports at launch"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""
    exec sleep infinity
fi
