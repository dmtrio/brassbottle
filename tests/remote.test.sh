#!/bin/bash
# tests/remote.test.sh — host-runnable checks for the RFC 04 remote-access
# mechanism. Needs only yq (+ standard tools, no docker): validates the
# manifest plumbing expressions up.sh uses, the compose overlays, the
# firewall/wrapper port-range agreement, and pins mirrored expressions to
# the source files (drift guard). The end-to-end SSH/mosh/phone path is the
# manual smoke test (IMP 04 A5/B2 acceptance).

# SC2015 (`A && pass || fail` is not if-else): intentional — pass() is a
# bare echo and cannot fail, so the || arm only runs when the check fails.
# SC2016 (expressions don't expand in single quotes): intentional — the
# drift greps look for LITERAL ${...} strings in the sources.
# shellcheck disable=SC2015,SC2016

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "$SCRIPT_DIR"

command -v yq >/dev/null || { echo "SKIP: yq not installed"; exit 0; }

FAILURES=0
fail() { echo "  ✗ $1"; FAILURES=$((FAILURES + 1)); }
pass() { echo "  ✓ $1"; }

echo "── syntax"
for f in up.sh jump.sh tunnel.sh src/entrypoint.sh src/init-firewall.sh src/tmux-notify.sh src/mosh-server-wrapper.sh src/tmux-landing.bashrc src/freshness-landing.bashrc jump/entrypoint.sh; do
    bash -n "$f" && pass "bash -n $f" || fail "$f has syntax errors"
done
python3 -m py_compile src/tmux_landing_gc.py \
    && pass "python3 -m py_compile src/tmux_landing_gc.py" \
    || fail "src/tmux_landing_gc.py has syntax errors"
python3 -m py_compile src/remote_access.py \
    && pass "python3 -m py_compile src/remote_access.py" \
    || fail "src/remote_access.py has syntax errors"

echo "── remote_access.py (AGENTS.md \"Python over bash\" — START_SSHD +"
echo "  authorized_keys + the jump-scoped :22 rule live here, not inline bash)"
grep -qF '/usr/local/lib/djinn/remote_access.py sshd' src/entrypoint.sh \
    && pass "entrypoint.sh calls remote_access.py sshd" \
    || fail "src/entrypoint.sh no longer calls remote_access.py sshd"
grep -qF '/usr/local/lib/djinn/remote_access.py firewall-ssh' src/init-firewall.sh \
    && pass "init-firewall.sh calls remote_access.py firewall-ssh" \
    || fail "src/init-firewall.sh no longer calls remote_access.py firewall-ssh"
grep -qF 'COPY src/remote_access.py /usr/local/lib/djinn/remote_access.py' Dockerfile \
    && pass "Dockerfile installs remote_access.py into /usr/local/lib/djinn" \
    || fail "Dockerfile does not wire remote_access.py into /usr/local/lib/djinn"

echo "── jump container (PLN - Djinn Admin Plane PR 1)"
grep -qF 'COPY src/mosh-server-wrapper.sh /usr/local/bin/mosh-server' jump/Dockerfile \
    && pass "jump image reuses the bottle's mosh-server wrapper" \
    || fail "jump/Dockerfile does not install src/mosh-server-wrapper.sh"
grep -qF 'update-locale LANG=en_US.UTF-8' jump/Dockerfile \
    && pass "jump image sets a UTF-8 native locale (mosh-server aborts without one)" \
    || fail "jump/Dockerfile is missing the UTF-8 locale setup"
# The env var itself is read by remote_access.py now (Python over bash),
# not src/entrypoint.sh directly — see the '>> authorized_keys' pin below
# for the ordering guarantee.
grep -qF 'JUMP_AUTHORIZED_KEY' src/remote_access.py \
    && pass "remote_access.py honours JUMP_AUTHORIZED_KEY" \
    || fail "src/remote_access.py does not read JUMP_AUTHORIZED_KEY"
grep -qF 'JUMP_AUTHORIZED_KEY=${JUMP_AUTHORIZED_KEY:-}' compose/docker-compose.local.yml \
    && pass "local compose passes JUMP_AUTHORIZED_KEY into the bottle" \
    || fail "compose/docker-compose.local.yml does not pass JUMP_AUTHORIZED_KEY"
# The append itself moved into remote_access.py's _rebuild_authorized_keys
# (Python over bash — src/entrypoint.sh no longer touches the file directly),
# so pin the operator-then-jump ORDER there instead of the old
# '>> ...authorized_keys' literal, which no longer exists in entrypoint.sh.
grep -qF 'for k in (ssh_key, jump_key)' src/remote_access.py \
    && pass "jump key is appended AFTER the operator key, so the operator key keeps working" \
    || fail "src/remote_access.py must write the operator key before the jump key, never replace"

echo "── tunnel connector (PLN - Djinn Admin Plane, phase A)"
grep -qF 'DEFAULT_TUNNEL_IMAGE = "fosrl/newt:' src/tunnel_config.py \
    && pass "tunnel image is pinned to a version, never :latest" \
    || fail "src/tunnel_config.py does not pin the tunnel image"
grep -qF 'PANGOLIN_ENDPOINT' secrets.env.example \
    && pass "secrets.env.example documents the Pangolin credentials" \
    || fail "secrets.env.example is missing PANGOLIN_ENDPOINT"
# The rendered compose is what matters, not the source (whose docstring
# explains why these are absent). tests/test_tunnel_config.py asserts the render
# directly; here we pin the plumbing bash can see.
for v in PANGOLIN_ENDPOINT NEWT_ID NEWT_SECRET; do
    grep -qF -- "$v=\"\${$v:-}\"" tunnel.sh \
        && pass "tunnel.sh forwards $v from secrets.env" \
        || fail "tunnel.sh does not forward $v"
done
grep -qF 'DJINN_SUBNET="${DJINN_SUBNET:-}"' tunnel.sh \
    && pass "tunnel.sh forwards DJINN_SUBNET (common.sh exports nothing)" \
    || fail "tunnel.sh does not forward DJINN_SUBNET"

echo "── compose overlays"
for f in compose/docker-compose.local.yml compose/docker-compose.ssh.yml compose/docker-compose.mosh.yml; do
    yq '.' "$f" >/dev/null 2>&1 && pass "$f parses" || fail "$f is not valid YAML"
done
[ "$(yq '.networks.default.name' compose/docker-compose.local.yml)" = "djinn-net" ] \
    && pass "local compose joins the shared djinn-net bridge" \
    || fail "local compose is missing the shared-network config"
[ "$(yq '.networks.default.external' compose/docker-compose.local.yml)" = "true" ] \
    && pass "shared network is external (created by up.sh, not compose)" \
    || fail "shared network must be external: true"
for var in REMOTE_JUMP REMOTE_SHELL DJINN_JUMP_IP SSH_AUTHORIZED_KEY JUMP_AUTHORIZED_KEY NTFY_URL NTFY_TOPIC; do
    yq -r '.services.djinn.environment[]' compose/docker-compose.local.yml | grep -q "^$var=" \
        && pass "local compose passes $var" \
        || fail "local compose is missing $var"
done
yq -r '.services.djinn.environment[]' compose/docker-compose.ssh.yml | grep -q '^SSH_ENABLED=true$' \
    && pass "ssh overlay sets SSH_ENABLED=true" \
    || fail "ssh overlay is missing SSH_ENABLED=true"
[ "$(yq '.services.djinn.environment | length' compose/docker-compose.ssh.yml)" = "1" ] \
    && pass "ssh overlay carries ONLY SSH_ENABLED (keys/shell/notify moved to local compose)" \
    || fail "ssh overlay environment: has drifted from just SSH_ENABLED"

echo "── mosh port-range agreement (manifest.py is the source; defaults must align)"
# The overlay carries fallbacks (${MOSH_PORTS:-...} / ${MOSH_PORTS_DASH:-...})
# for the values manifest.py computes from remote.mosh_ports. All defaults — env
# (colon form), publish (dash form), wrapper, manifest.py — must be one range.
ENV_DEFAULT=$(yq -r '.services.djinn.environment[]' compose/docker-compose.mosh.yml | sed -n 's/^MOSH_PORTS=${MOSH_PORTS:-\(.*\)}$/\1/p')
[ "$ENV_DEFAULT" = "60000:60010" ] \
    && pass "mosh overlay env default is 60000:60010" \
    || fail "mosh overlay env default unexpected: '$ENV_DEFAULT'"
DASH_DEFAULT=$(yq -r '.services.djinn.ports[0]' compose/docker-compose.mosh.yml | grep -o '{MOSH_PORTS_DASH:-[0-9-]*}' | head -1 | sed 's/.*:-\([0-9-]*\)}/\1/')
[ "$DASH_DEFAULT" = "${ENV_DEFAULT/:/-}" ] \
    && pass "publish default ($DASH_DEFAULT) matches env default" \
    || fail "publish default '$DASH_DEFAULT' != env default '${ENV_DEFAULT/:/-}'"
grep -qF "\${MOSH_PORTS:-$ENV_DEFAULT}" src/mosh-server-wrapper.sh \
    && pass "mosh-server wrapper default matches the overlay default" \
    || fail "wrapper default range drifted from the overlay"
grep -qF '"60000:60010"' src/manifest.py \
    && pass "manifest.py default range matches the overlay" \
    || fail "manifest.py default range drifted"

# manifest.py's remote.mosh_ports validation (MOSH_PORTS_RE) must reject malformed/reversed ranges
check_range() { printf '%s' "$1" | grep -qE '^[0-9]{1,5}:[0-9]{1,5}$'; }
check_range "60000:60010" && pass "range validation accepts 60000:60010" || fail "validation rejects the default range"
check_range "60000-60010" && fail "range validation accepted dash form" || pass "range validation rejects dash form"
check_range "abc:123"     && fail "range validation accepted junk" || pass "range validation rejects junk"

# wrapper: the -p pin must be spliced BEFORE any '--' (a trailing pin lands
# in the remote command's argv and is silently ignored by getopt)
grep -qF 'exec /usr/bin/mosh-server new "${ARGS[@]}"' src/mosh-server-wrapper.sh \
    && pass "wrapper rebuilds argv around 'new'" \
    || fail "wrapper argv splice missing"
awk '/for a in "\$@"/,/^fi$/' src/mosh-server-wrapper.sh | grep -qF '"--"' \
    && pass "wrapper splices the pin before '--'" \
    || fail "wrapper no longer handles the '--' separator"

echo "── manifest plumbing simulation (same expressions as up.sh)"
M=$(mktemp); trap 'rm -f "$M"' EXIT
printf 'ssh:\n  port: 2222\nremote:\n  jump: false\n  shell: bash\n  mosh: true\n  notify: ntfy\n' > "$M"
# Read directly, no `// true` default: yq/jq's `//` treats `false` itself as
# falsy and would silently substitute the default, masking exactly the
# opt-out case (remote.jump: false) this is meant to prove reads back
# correctly — same pitfall manifest.py's own reader must avoid (it keys the
# default off "jump" not in remote, not off falsiness).
[ "$(yq '.remote.jump' "$M")" = "false" ]   && pass "remote.jump reads back"   || fail "remote.jump read broken"
[ "$(yq -r '.remote.shell // "tmux"' "$M")" = "bash" ] && pass "remote.shell reads back" || fail "remote.shell read broken"
[ "$(yq '.remote.mosh // false' "$M")" = "true" ]  && pass "remote.mosh reads back"  || fail "remote.mosh read broken"
[ "$(yq -r '.remote.notify // ""' "$M")" = "ntfy" ] && pass "remote.notify reads back" || fail "remote.notify read broken"

# ntfy host extraction (same sed as up.sh; drift-guarded below)
extract_host() { printf '%s' "$1" | sed -E 's|^[A-Za-z]+://||; s|/.*$||; s|^.*@||; s|:[0-9]+$||'; }
[ "$(extract_host 'https://ntfy.example.com')" = "ntfy.example.com" ] \
    && pass "ntfy host parsed from bare https URL" \
    || fail "host extraction broke on bare URL"
[ "$(extract_host 'http://ntfy.lan:8080/topic')" = "ntfy.lan" ] \
    && pass "ntfy host parsed from URL with port + path" \
    || fail "host extraction broke on port/path URL"
[ "$(extract_host 'https://user:pass@ntfy.example.com')" = "ntfy.example.com" ] \
    && pass "userinfo is stripped (not mistaken for the host)" \
    || fail "host extraction broke on userinfo URL: got '$(extract_host 'https://user:pass@ntfy.example.com')'"
[ "$(extract_host 'http://192.168.1.50:8080')" = "192.168.1.50" ] \
    && pass "IP-literal host parses intact" \
    || fail "host extraction broke on IP-literal URL"
# IP literals must take the CIDR path (DNS-driven zones never see them)
printf '%s' "192.168.1.50" | grep -qE '^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$' \
    && pass "IP-literal detection matches (routes to egress_cidrs)" \
    || fail "IP-literal detection regex broken"
printf '%s' "ntfy.example.com" | grep -qE '^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$' \
    && fail "hostname misdetected as IP literal" \
    || pass "hostnames stay on the zone path"

echo "── landing + notify wiring"
grep -qF '${REMOTE_SHELL:-tmux}' src/tmux-landing.bashrc \
    && pass "landing snippet gates on REMOTE_SHELL (tmux default)" \
    || fail "landing snippet lost the REMOTE_SHELL gate"
grep -qF 'login-' src/tmux-landing.bashrc \
    && pass "landing snippet names fresh sessions with login-* prefix" \
    || fail "landing snippet lost login-* fresh-session naming"
grep -qF 'choose-tree -Zs' src/tmux-landing.bashrc \
    && pass "landing snippet opens picker when other sessions exist" \
    || fail "landing snippet lost choose-tree picker launch"
grep -qF '/usr/local/lib/djinn/tmux_landing_gc.py' src/tmux-landing.bashrc \
    && pass "landing snippet runs tmux landing GC before creating a session" \
    || fail "landing snippet lost tmux landing GC invocation"
grep -qE 'sshd\|sshd-session\|mosh-server' src/tmux-landing.bashrc \
    && pass "landing gates on sshd/sshd-session/mosh-server parents (OpenSSH >=9.8 split)" \
    || fail "landing snippet lost the parent-process gate (must include sshd-session)"
grep -qF '/proc/$PPID/comm' src/tmux-landing.bashrc \
    && pass "parent check reads /proc directly (no procps dependency)" \
    || fail "parent check no longer reads /proc/\$PPID/comm"
grep -qF 'TERM_PROGRAM' src/tmux-landing.bashrc \
    && pass "landing supports interactive TERM_PROGRAM=vscode terminals" \
    || fail "landing snippet lost TERM_PROGRAM=vscode gate"
grep -qF 'tmux-landing.bashrc' Dockerfile \
    && pass "Dockerfile installs + sources the landing snippet" \
    || fail "Dockerfile no longer wires tmux-landing.bashrc"
grep -qF 'COPY src/tmux_landing_gc.py /usr/local/lib/djinn/tmux_landing_gc.py' Dockerfile \
    && pass "Dockerfile installs tmux_landing_gc.py into /usr/local/lib/djinn" \
    || fail "Dockerfile does not wire tmux_landing_gc.py into /usr/local/lib/djinn"
grep -qF '/usr/local/bin/tmux-notify.sh' src/tmux.conf \
    && pass "tmux.conf silence hook points at tmux-notify.sh" \
    || fail "tmux.conf hook target drifted"
grep -qF '/usr/local/lib/djinn/tmux_landing_gc.py' src/tmux.conf \
    && pass "tmux.conf triggers landing GC on detach/switch hooks" \
    || fail "tmux.conf lost landing GC hook wiring"
grep -qF 'src/tmux-notify.sh /usr/local/bin/tmux-notify.sh' Dockerfile \
    && pass "Dockerfile installs tmux-notify.sh where the hook expects" \
    || fail "Dockerfile install path drifted from the tmux.conf hook"
grep -qF 'monitor-silence' src/tmux.conf \
    && pass "tmux.conf arms monitor-silence behind NTFY_URL" \
    || fail "tmux.conf lost the silence monitor"
grep -qF 'silence-action any' src/tmux.conf \
    && pass "silence-action any set (default 'other' never fires for a single-window session)" \
    || fail "tmux.conf lost silence-action any — the notifier would never fire"
grep -qF '#{hook_window}' src/tmux.conf \
    && pass "hook passes the alerting window to the notifier" \
    || fail "tmux.conf hook no longer passes #{hook_window}"
grep -qF 'TARGET="${1:-}"' src/tmux-notify.sh \
    && pass "notifier requires the hook-window target argument" \
    || fail "notifier target handling drifted (no durable session to fall back to)"
grep -qF 'run-shell -b' src/tmux.conf \
    && pass "GC hooks run backgrounded with output redirected" \
    || fail "tmux.conf GC hooks lost run-shell -b (blocking pager in attached clients)"
grep -qF '"kill-session", "-t", f"={name}"' src/tmux_landing_gc.py \
    && pass "GC kills with exact-match (=) targets" \
    || fail "GC lost exact-match kill targets — prefix matching can kill live sessions"
grep -qF 'list-clients' src/tmux-notify.sh \
    && pass "notifier suppresses while a client is attached" \
    || fail "notifier lost attached-client suppression"

echo "── drift guard (expressions this suite mirrors must exist in the sources)"
while IFS=$'\t' read -r file expr; do
    [ -n "$expr" ] || continue
    grep -qF -- "$expr" "$file" \
        && pass "$file still contains: $expr" \
        || fail "$file no longer contains (update this suite!): $expr"
done <<'DRIFT'
src/manifest.py	remote.get("tmux")
src/manifest.py	remote.get("jump")
src/manifest.py	remote.get("shell")
src/manifest.py	remote.get("mosh")
src/manifest.py	remote.get("notify")
src/manifest.py	remote.get("mosh_ports")
up.sh	compose/docker-compose.mosh.yml
src/ensure_net.py	djinn-net
src/manifest.py	remote.notify requires remote.shell: tmux
src/manifest.py	re.sub(r"^[A-Za-z]+://", "", ntfy_url)
src/manifest.py	[0-9]{1,5}:[0-9]{1,5}
src/init-firewall.sh	^[0-9]+:[0-9]+$
src/init-firewall.sh	--dport "$MOSH_PORTS"
src/init-firewall.sh	-s "$HOST_IP"
src/init-firewall.sh	-s "$DJINN_JUMP_IP"
src/entrypoint.sh	REMOTE_SHELL MOSH_PORTS NTFY_URL NTFY_TOPIC CONTAINER_NAME
Dockerfile	update-locale LANG=en_US.UTF-8
DRIFT

echo ""
if [ "$FAILURES" -gt 0 ]; then
    echo "FAILED: $FAILURES check(s)"
    exit 1
fi
echo "all checks passed"
