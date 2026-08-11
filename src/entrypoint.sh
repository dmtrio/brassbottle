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
# to mkdir its own path at build. Non-recursive on purpose: only a freshly
# created volume can be wrong, and it is empty by definition — a recursive chown
# would walk a cache that can reach hundreds of MB on every boot.
for _vol_path in ${PLUGIN_VOLUME_PATHS:-}; do
    mkdir -p "$_vol_path" && chown coder:coder "$_vol_path" \
        && echo "✓ Plugin volume: $_vol_path" \
        || echo "⚠ Plugin volume $_vol_path: could not fix ownership"
done
unset _vol_path

# ── Egress firewall (default ON — fail loud, never run open) ─────────────────
if [ "${ENABLE_FIREWALL:-true}" = "true" ]; then
    if /usr/local/bin/init-firewall.sh; then
        echo "✓ Egress firewall active"
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
if [ "$SSH_ENABLED" = "true" ]; then
    # Runtime key injection — same image everywhere, key comes from the
    # manifest deploy (SSH_AUTHORIZED_KEY in secrets.env). Fail loud rather
    # than start sshd nobody can log into.
    if [ -z "$SSH_AUTHORIZED_KEY" ]; then
        echo "FATAL: SSH_ENABLED=true but SSH_AUTHORIZED_KEY is empty."
        echo "Set SSH_AUTHORIZED_KEY in ~/dev-agent/secrets.env (your public key)."
        exit 1
    fi
    echo "$SSH_AUTHORIZED_KEY" > /home/coder/.ssh/authorized_keys
    chmod 600 /home/coder/.ssh/authorized_keys
    chown coder:coder /home/coder/.ssh/authorized_keys

    if [ ! -f /etc/ssh/ssh_host_rsa_key ]; then
        echo "Generating SSH host keys..."
        ssh-keygen -A
        echo "✓ SSH host keys generated"
    fi

    # RFC 04: sshd builds each session's env via PAM (/etc/environment) and
    # ignores container env — persist the remote-access vars there so login
    # shells (and the tmux server/hooks they start) can see them. mosh
    # sessions inherit from their SSH bootstrap, so this covers both.
    for var in REMOTE_TMUX MOSH_PORTS NTFY_URL NTFY_TOPIC CONTAINER_NAME; do
        val="${!var:-}"
        [ -n "$val" ] || continue
        sed -i "/^$var=/d" /etc/environment
        echo "$var=$val" >> /etc/environment
    done
    [ "${REMOTE_TMUX:-false}" = "true" ] && echo "✓ Remote access: SSH/mosh logins land in tmux session 'agent'"

    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "  Container:  dev-agent-${CONTAINER_NAME}   (sshd on :22, published"
    echo "              on the host at the manifest's ssh.port)"
    echo "  SSH:        ssh -p <ssh.port> coder@<docker-host>"
    echo "  VS Code:    Remote-SSH to the same host/port"
    echo "  Workspace:  /workspace"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""

    echo "Starting sshd..."
    exec /usr/sbin/sshd -D
else
    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "  Container:  dev-agent-${CONTAINER_NAME}   (attach mode, no sshd)"
    echo ""
    echo "  VS Code / Cursor:"
    echo "    Dev Containers: Attach to Running Container → dev-agent-${CONTAINER_NAME}"
    echo "    then open /workspace (or /workspace/dev.code-workspace)"
    echo ""
    echo "  Terminal:   docker exec -it -u coder dev-agent-${CONTAINER_NAME} bash"
    echo "  Workspace:  /workspace"
    echo ""
    echo "  Dev servers: use VS Code port forwarding, or publish ports at launch"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""
    exec sleep infinity
fi
