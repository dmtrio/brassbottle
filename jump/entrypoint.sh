#!/bin/bash
# jump-entrypoint.sh — in-container startup for the singleton jump container.
#
# Boundary logging (working agreement): every stage announces itself, because
# this process is the only thing standing between a phone and the fleet and a
# silent failure here looks exactly like "the tunnel is down".
set -euo pipefail

SSH_DIR="${SSH_DIR:-/etc/djinn-jump/ssh}"
CLIENT_KEY="$SSH_DIR/id_ed25519"
KNOWN_HOSTS="$SSH_DIR/known_hosts"

log() { printf '%s jump %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

log "start begin ssh_dir=$SSH_DIR mosh_ports=${MOSH_PORTS:-unset}"

if [ -z "${SSH_AUTHORIZED_KEY:-}" ]; then
    log "start error reason=SSH_AUTHORIZED_KEY empty"
    echo "FATAL: SSH_AUTHORIZED_KEY is empty — set your public key in secrets.env." >&2
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

printf '%s\n' "$SSH_AUTHORIZED_KEY" > /home/coder/.ssh/authorized_keys
chmod 600 /home/coder/.ssh/authorized_keys
chown coder:coder /home/coder/.ssh/authorized_keys
log "authorized_keys written bytes=${#SSH_AUTHORIZED_KEY}"

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
