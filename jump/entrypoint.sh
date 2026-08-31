#!/bin/bash
# jump-entrypoint.sh — in-container startup for the singleton jump container.
#
# Boundary logging (working agreement): every stage announces itself, because
# this process is the only thing standing between a phone and the fleet and a
# silent failure here looks exactly like "the tunnel is down".
set -euo pipefail

SSH_DIR="${SSH_DIR:-/etc/djinn-jump/ssh}"
AUTHORIZED_KEYS_SRC="${AUTHORIZED_KEYS_SRC:-/etc/djinn-jump/authorized_keys}"
CLIENT_KEY="$SSH_DIR/id_ed25519"
KNOWN_HOSTS="$SSH_DIR/known_hosts"

log() { printf '%s jump %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

log "start begin ssh_dir=$SSH_DIR keys_src=$AUTHORIZED_KEYS_SRC mosh_ports=${MOSH_PORTS:-unset}"

# The key list arrives as a read-only FILE bind mount, so multiple keys need
# no env var and no YAML scalar carrying newlines. Docker silently creates a
# DIRECTORY when a bind-mount source is missing, so check for a regular file
# specifically — "is a directory" here would otherwise become an unreadable
# authorized_keys and a phone that cannot log in for no visible reason.
if [ ! -f "$AUTHORIZED_KEYS_SRC" ]; then
    log "start error reason=authorized_keys-not-a-file path=$AUTHORIZED_KEYS_SRC"
    echo "FATAL: $AUTHORIZED_KEYS_SRC is missing or not a regular file." >&2
    echo "Add one public key per line to \$DJINN_HOME/jump/authorized_keys." >&2
    exit 1
fi

# The mount is the ONLY persistent state. Everything below is idempotent so a
# recreate reuses the same host keys (no phone MITM warning) and the same
# client key (bottles keep trusting the key they already authorised).
mkdir -p "$SSH_DIR"
chown coder:coder "$SSH_DIR"
# 0755, not 0700: this is a bind mount, and `./djinn jump pubkey` reads the
# public key from the HOST side as the operator's uid. 0700 owned by uid 1000
# makes the directory untraversable there. The private keys inside stay 0600,
# which is what sshd and ssh actually check.
chmod 755 "$SSH_DIR"

# COPIED, not pointed at directly: sshd's StrictModes rejects an
# authorized_keys owned by neither root nor the login user, and a bind-mounted
# host file carries whatever uid the host maps it to. Copying gives it a known
# owner and mode. Cost: adding a key needs a jump restart, not just an edit.
install -o coder -g coder -m 600 "$AUTHORIZED_KEYS_SRC" /home/coder/.ssh/authorized_keys
# `|| true` because grep exits 1 on zero matches, which set -e would kill.
# The :-0 default covers an exit 2 (grep itself failed), where stdout is empty
# and a bare [ "" -eq 0 ] would be a syntax error instead of a clear message.
key_count=$(grep -cve '^[[:space:]]*$' -e '^[[:space:]]*#' /home/coder/.ssh/authorized_keys || true)
key_count=${key_count:-0}
if [ "$key_count" -eq 0 ]; then
    log "start error reason=authorized_keys-empty path=$AUTHORIZED_KEYS_SRC"
    echo "FATAL: $AUTHORIZED_KEYS_SRC contains no keys — sshd would accept nobody." >&2
    exit 1
fi
log "authorized_keys installed keys=$key_count"

# Host keys live on the mount, not in the image layer: a regenerated host key
# is a scary warning on the operator's phone every time the jump is rebuilt.
host_keys_generated=0
for type in rsa ecdsa ed25519; do
    key="$SSH_DIR/ssh_host_${type}_key"
    if [ ! -f "$key" ]; then
        ssh-keygen -q -t "$type" -N "" -f "$key" </dev/null
        host_keys_generated=$((host_keys_generated + 1))
    fi
    chmod 600 "$key"
done
log "host keys ready generated=$host_keys_generated"

# The jump's own client key — what bottles authorise as JUMP_AUTHORIZED_KEY.
if [ ! -f "$CLIENT_KEY" ]; then
    ssh-keygen -q -t ed25519 -N "" -C "djinn-jump" -f "$CLIENT_KEY" </dev/null
    log "client key generated path=$CLIENT_KEY"
else
    log "client key reused path=$CLIENT_KEY"
fi
chmod 600 "$CLIENT_KEY"
chmod 644 "$CLIENT_KEY.pub"
chown coder:coder "$CLIENT_KEY" "$CLIENT_KEY.pub"

touch "$KNOWN_HOSTS"
chown coder:coder "$KNOWN_HOSTS"
chmod 644 "$KNOWN_HOSTS"

cat > /etc/ssh/sshd_config.d/djinn-jump.conf <<SSHD
Port 22
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
AllowUsers coder
AcceptEnv LANG LC_*
HostKey $SSH_DIR/ssh_host_rsa_key
HostKey $SSH_DIR/ssh_host_ecdsa_key
HostKey $SSH_DIR/ssh_host_ed25519_key
SSHD

# PAM builds each session's env from /etc/environment and ignores container
# env — same reason src/entrypoint.sh does this for the bottles. Without it
# the mosh-server wrapper cannot see MOSH_PORTS and falls back to its default.
#
# touch first: debian:bookworm-slim does not ship /etc/environment (it is
# dpkg-unowned), and the bottle image only gets away with the same loop
# because its Dockerfile writes the file explicitly. Under `set -e` a `sed -i`
# on a missing file exits 2 and kills the entrypoint before sshd ever starts —
# a permanent restart loop that would first surface on the operator's phone.
touch /etc/environment
for var in MOSH_PORTS LANG LC_ALL; do
    val="${!var:-}"
    [ -n "$val" ] || continue
    sed -i "/^$var=/d" /etc/environment
    echo "$var=$val" >> /etc/environment
done

cat > /etc/motd <<'MOTD'

  djinn jump — hop onward to a bottle by container name:

      ssh djinn-<bottle>          # e.g. ssh djinn-coding-tanks

  The bottle's own tmux session ('agent') is what persists your work;
  this container only carries the mosh leg from your phone.

MOTD

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  djinn-jump is up (sshd :22, mosh UDP ${MOSH_PORTS:-60000:60010})"
echo ""
echo "  Authorise this key in your bottles — set in secrets.env:"
# QUOTED: secrets.env is sourced by every host script under `set -e`, so an
# unquoted key's spaces would make bash run its comment as a command and abort
# every ./djinn up. Printing the line the operator copies is exactly where
# that has to be right.
echo "    JUMP_AUTHORIZED_KEY=\"$(cat "$CLIENT_KEY.pub")\""
echo "  then re-run ./djinn up <bottle> for each bottle you want reachable."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

log "start ok sshd=starting"
exec /usr/sbin/sshd -D -e
