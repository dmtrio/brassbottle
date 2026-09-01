#!/bin/bash
# init-firewall.sh — egress allowlist for agent dev containers.
# Adapted from anthropics/claude-code .devcontainer/init-firewall.sh.
#
# Default-denies all outbound traffic except an ipset allowlist (GitHub IP
# ranges + dnsmasq-resolved zones), DNS to the container's own resolvers,
# and loopback. Verifies itself at the end (including a dnsmasq-only zone)
# and exits non-zero on any failure — the entrypoint treats that as fatal
# so the container never runs with open egress.
#
# Requires: NET_ADMIN + NET_RAW capabilities; iptables, ipset, dig, jq,
# aggregate, curl (installed in the Dockerfile).
#
# Env:
#   EXTRA_ALLOWED_DOMAINS  comma/space-separated extra zones to allow
#                          (a zone covers itself and all subdomains)
#   ALLOWED_CIDRS          comma/space-separated IP ranges to allow
#                          (e.g. LAN subnets: 192.168.35.0/24)
#   HOST_MCP_PORTS         comma/space-separated TCP ports on
#                          host.docker.internal to open (MCP servers on the
#                          host). Unset = host unreachable.
#   ENABLE_EGRESS_BROKER   when true (default), redirect blocked :80/:443 to
#                          the in-container broker, log via NFLOG group 32,
#                          and start the transparent broker (entrypoint).
#   REMOTE_JUMP            default-on jump reachability (PLN - default jump
#                          reachability). true (default) scopes an inbound
#                          :22 ACCEPT to DJINN_JUMP_IP when SSH_ENABLED is not
#                          also set; false skips the rule entirely (opt-out).
#                          Decision (open/jump/none) + validation of
#                          DJINN_JUMP_IP is made by remote_access.py
#                          firewall-ssh, evaled below.
#   DJINN_JUMP_IP          the jump container's static bridge address (up.sh
#                          resolves it host-side via `jump_host.py ip`).
#                          Empty = warn and skip the rule — sshd, if the
#                          entrypoint started it, is unreachable until the
#                          address is known.

set -euo pipefail
IFS=$'\n\t'

# 1. Extract Docker DNS info BEFORE any flushing
DOCKER_DNS_RULES=$(iptables-save -t nat | grep "127\.0\.0\.11" || true)

# Flush existing rules and delete existing ipsets
iptables -F
iptables -X
iptables -t nat -F
iptables -t nat -X
iptables -t mangle -F
iptables -t mangle -X
ipset destroy allowed-domains 2>/dev/null || true

# 2. Selectively restore ONLY internal Docker DNS resolution
if [ -n "$DOCKER_DNS_RULES" ]; then
    echo "Restoring Docker DNS rules..."
    iptables -t nat -N DOCKER_OUTPUT 2>/dev/null || true
    iptables -t nat -N DOCKER_POSTROUTING 2>/dev/null || true
    echo "$DOCKER_DNS_RULES" | xargs -L 1 iptables -t nat
else
    echo "No Docker DNS rules to restore"
fi

# First allow DNS and localhost before any restrictions
iptables -A OUTPUT -p udp --dport 53 -d 127.0.0.11 -j ACCEPT
iptables -A OUTPUT -p udp --dport 53 -d 127.0.0.1 -j ACCEPT
iptables -A INPUT -p udp --sport 53 -j ACCEPT
# Localhost
iptables -A INPUT -i lo -j ACCEPT
iptables -A OUTPUT -o lo -j ACCEPT

# Inbound SSH — decided and PARTLY enforced here, right after the loopback
# ACCEPTs and BEFORE the gateway ACCEPT added below (HOST_IP, ALL ports): on
# a Linux host the gateway IS the docker host, so that wide-open gateway rule
# would otherwise let the operator reach a jump-only bottle's :22 straight
# through the gateway — bypassing the jump entirely and contradicting
# "reachable ONLY from the jump". Because iptables evaluates INPUT rules in
# the order they were added, a DROP added here (for every mode except
# `open`, which is meant to be wide-open) sits ahead of that gateway ACCEPT
# and wins first. `--state NEW` only — never ESTABLISHED,RELATED: a jump
# session that is already flowing must keep passing even though this rule's
# source test would otherwise match its packets too; the
# ESTABLISHED,RELATED ACCEPT added further below (after the gateway rule)
# covers those packets, so this rule only ever needs to stop a NEW connection
# attempt from opening in the first place. remote_access.py owns the
# open/jump/none decision and DJINN_JUMP_IP validation (AGENTS.md "Python
# over bash"); captured into a variable BEFORE eval, not `eval "$(…)"`
# inline — a failed command substitution's exit status is lost once `eval`
# runs on its (empty) output, which would silently swallow the invalid-IP
# fail-closed case under this script's `set -euo pipefail`. The
# corresponding ACCEPT rules for this same decision are added further down
# (after the gateway/ESTABLISHED rules, where SSH inbound has always been
# decided) — SSH_INPUT_RULE and DJINN_JUMP_IP are re-used from this same
# eval there, not re-derived.
FIREWALL_SSH_ARGS=$(python3 /usr/local/lib/djinn/remote_access.py firewall-ssh)
eval "$FIREWALL_SSH_ARGS"
case "$SSH_INPUT_RULE" in
    jump)
        echo "Pre-gateway: dropping new inbound :22 except from jump $DJINN_JUMP_IP"
        iptables -A INPUT -p tcp --dport 22 -m state --state NEW ! -s "$DJINN_JUMP_IP" -j DROP
        ;;
    none)
        echo "Pre-gateway: dropping all new inbound :22 (no jump, no published ssh)"
        iptables -A INPUT -p tcp --dport 22 -m state --state NEW -j DROP
        ;;
    open)
        ;;
    *)
        echo "ERROR: remote_access.py returned an unknown SSH_INPUT_RULE: $SSH_INPUT_RULE"
        exit 1
        ;;
esac

# Create ipset with CIDR support
ipset create allowed-domains hash:net

# Fetch GitHub meta information and aggregate + add their IP ranges
echo "Fetching GitHub IP ranges..."
# Bounded — a DNS/network stall here would otherwise hang the entrypoint
# before the firewall is up (and up.sh's readiness wait would burn its full
# timeout). Fail fast instead.
gh_ranges=$(curl -s --connect-timeout 5 --max-time 15 https://api.github.com/meta)
if [ -z "$gh_ranges" ]; then
    echo "ERROR: Failed to fetch GitHub IP ranges"
    exit 1
fi

if ! echo "$gh_ranges" | jq -e '.web and .api and .git' >/dev/null; then
    echo "ERROR: GitHub API response missing required fields"
    exit 1
fi

echo "Processing GitHub IPs..."
while read -r cidr; do
    if [[ ! "$cidr" =~ ^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}/[0-9]{1,2}$ ]]; then
        echo "ERROR: Invalid CIDR range from GitHub meta: $cidr"
        exit 1
    fi
    ipset add allowed-domains "$cidr"
done < <(echo "$gh_ranges" | jq -r '(.web + .api + .git)[]' | aggregate -q)

# Allowed ZONES (a zone covers itself and every subdomain). Enforcement is
# resolver-driven: dnsmasq adds every IP it resolves for these zones to the
# ipset at lookup time, so rotating/geo DNS (Cursor, CDNs) can't outrun the
# firewall. Agents: claude, codex, cursor, pi (+ aider via provider
# zones). Plus package registries, GitHub assets, apt, VS Code server, and
# Playwright browser downloads (playwright is in the base image; the standing
# "visual check on every step" rule needs a working browser in every container).
ALLOWED_ZONES="
anthropic.com
claude.ai
sentry.io
statsig.com
openai.com
chatgpt.com
cursor.com
cursor.sh
cursorapi.com
pi.dev
andmakenomistakes.com
npmjs.org
nodejs.org
pypi.org
pythonhosted.org
github.com
githubusercontent.com
ubuntu.com
visualstudio.com
vscode.download.prss.microsoft.com
vsassets.io
cdn.playwright.dev
playwright.download.prss.microsoft.com
"

# Per-container additions
if [ -n "${EXTRA_ALLOWED_DOMAINS:-}" ]; then
    echo "Adding extra allowed zones: $EXTRA_ALLOWED_DOMAINS"
    ALLOWED_ZONES="$ALLOWED_ZONES
$(echo "$EXTRA_ALLOWED_DOMAINS" | tr ', ' '\n\n')"
fi

# dnsmasq: forward to Docker's embedded DNS, mirror answers for allowed
# zones into the ipset. All container DNS goes through it via resolv.conf.
{
    echo "no-resolv"
    echo "server=127.0.0.11"
    echo "listen-address=127.0.0.1"
    echo "bind-interfaces"
    echo "cache-size=1000"
    for z in $ALLOWED_ZONES; do
        [ -z "$z" ] && continue
        echo "ipset=/$z/allowed-domains"
    done
} > /etc/dnsmasq.conf

pkill -x dnsmasq 2>/dev/null || true
dnsmasq --conf-file=/etc/dnsmasq.conf
sleep 1
if ! dig +time=3 +tries=1 @127.0.0.1 api.github.com >/dev/null 2>&1; then
    echo "ERROR: dnsmasq failed to start or resolve"
    exit 1
fi
echo "nameserver 127.0.0.1" > /etc/resolv.conf
echo "dnsmasq resolver active ($(grep -c '^ipset=' /etc/dnsmasq.conf) zones mirrored to ipset)"

# Per-container CIDR escape hatch (e.g. LAN subnets for local-network services).
# hash:net ipsets take CIDRs directly — no DNS involved.
if [ -n "${ALLOWED_CIDRS:-}" ]; then
    for cidr in $(echo "$ALLOWED_CIDRS" | tr ', ' '\n\n'); do
        [ -z "$cidr" ] && continue
        if [[ ! "$cidr" =~ ^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}/[0-9]{1,2}$ ]]; then
            echo "ERROR: Invalid CIDR in ALLOWED_CIDRS: $cidr"
            exit 1
        fi
        echo "Allowing CIDR $cidr"
        ipset add allowed-domains "$cidr"
    done
fi

# Get host IP from default route
HOST_IP=$(ip route | grep default | cut -d" " -f3)
if [ -z "$HOST_IP" ]; then
    echo "ERROR: Failed to detect host IP"
    exit 1
fi

HOST_NETWORK=$(echo "$HOST_IP" | sed "s/\.[0-9]*$/.0\/24/")
echo "Host network detected as: $HOST_NETWORK"

# INBOUND from the GATEWAY IP stays open (published-port traffic arrives via
# the gateway proxy, whose source is the gateway address). Deliberately NOT
# the whole subnet: on the shared djinn-net bridge the /24 is the entire
# fleet, and a subnet-wide ACCEPT would let any sibling container reach this
# one's listeners — cross-container isolation must not rest on the sibling's
# own (agent-editable) OUTPUT chain. OUTBOUND to the host network is likewise
# NOT opened wholesale — a blanket rule would defeat the HOST_MCP_PORTS
# opt-in below.
iptables -A INPUT -s "$HOST_IP" -j ACCEPT

# Set default policies to DROP first
iptables -P INPUT DROP
iptables -P FORWARD DROP
iptables -P OUTPUT DROP

# Allow established connections for already approved traffic
iptables -A INPUT -m state --state ESTABLISHED,RELATED -j ACCEPT
iptables -A OUTPUT -m state --state ESTABLISHED,RELATED -j ACCEPT

# Allow inbound SSH: the published path (any source reaching :22, fronted by
# the operator's WireGuard/VPN tunnel today) when ssh: is explicit, else the
# default jump path scoped to ONE source IP — the jump container's static
# bridge address — because that path has no host port and no tunnel of its
# own narrowing who can reach it. Never both: an explicit ssh: bottle already
# gets the wide-open published rule, so a jump-scoped rule on top would only
# suggest a narrower path exists when it doesn't. SSH_INPUT_RULE and
# DJINN_JUMP_IP were already decided by remote_access.py firewall-ssh right
# after the loopback ACCEPTs above (see that comment for why it runs there,
# ahead of the gateway ACCEPT) — reused here, not re-derived, so the
# pre-gateway DROP and this ACCEPT always agree.
case "$SSH_INPUT_RULE" in
    open)
        echo "Allowing inbound :22 (published)"
        iptables -A INPUT -p tcp --dport 22 -j ACCEPT
        ;;
    jump)
        echo "Allowing inbound :22 from jump $DJINN_JUMP_IP"
        iptables -A INPUT -s "$DJINN_JUMP_IP" -p tcp --dport 22 -j ACCEPT
        ;;
    none)
        ;;
    *)
        echo "ERROR: remote_access.py returned an unknown SSH_INPUT_RULE: $SSH_INPUT_RULE"
        exit 1
        ;;
esac

# Inbound mosh UDP range when enabled (RFC 04). Set by the mosh compose
# overlay; reached only over the operator's WireGuard/VPN tunnel — the
# range is never published on a public interface.
if [ "${SSH_ENABLED:-false}" = "true" ] && [ -n "${MOSH_PORTS:-}" ]; then
    if [[ ! "$MOSH_PORTS" =~ ^[0-9]+:[0-9]+$ ]]; then
        echo "ERROR: Invalid MOSH_PORTS (want START:END): $MOSH_PORTS"
        exit 1
    fi
    echo "Allowing inbound mosh UDP $MOSH_PORTS"
    iptables -A INPUT -p udp --dport "$MOSH_PORTS" -j ACCEPT
fi

# Allow outbound traffic to allowed domains
iptables -A OUTPUT -m set --match-set allowed-domains dst -j ACCEPT

# Host MCP opt-in: open ONLY the listed TCP ports on host.docker.internal
if [ -n "${HOST_MCP_PORTS:-}" ]; then
    HOST_GW_IP=$(getent ahostsv4 host.docker.internal | awk 'NR==1{print $1}' || true)
    if [ -z "$HOST_GW_IP" ]; then
        echo "ERROR: HOST_MCP_PORTS set but host.docker.internal does not resolve"
        exit 1
    fi
    for port in $(echo "$HOST_MCP_PORTS" | tr ', ' '\n\n'); do
        [ -z "$port" ] && continue
        if [[ ! "$port" =~ ^[0-9]+$ ]]; then
            echo "ERROR: Invalid port in HOST_MCP_PORTS: $port"
            exit 1
        fi
        echo "Allowing host MCP port $HOST_GW_IP:$port"
        iptables -A OUTPUT -d "$HOST_GW_IP" -p tcp --dport "$port" -j ACCEPT
    done
fi

# Transparent egress broker (B3): REDIRECT blocked :80/:443 to the local
# listener, plus a companion filter ACCEPT for the redirected 127.0.0.1:3128
# destination (the generic -o lo ACCEPT does not cover REDIRECT targets).
# Disabled together with the broker process when ENABLE_EGRESS_BROKER=false.
if [ "${ENABLE_EGRESS_BROKER:-true}" = "true" ]; then
    # Broker filter ACCEPT must precede NFLOG: redirected connections already
    # show dst=127.0.0.1:3128 and would flood the operator queue as bogus filings.
    /usr/local/bin/egress_broker_firewall.sh add
    iptables -A OUTPUT -j NFLOG --nflog-group 32 --nflog-prefix "djinn-egress"
fi

# Explicitly REJECT all other outbound traffic for immediate feedback
iptables -A OUTPUT -j REJECT --reject-with icmp-admin-prohibited

echo "Firewall configuration complete"
echo "Verifying firewall rules..."
if curl --connect-timeout 5 https://example.com >/dev/null 2>&1; then
    echo "ERROR: Firewall verification failed - was able to reach https://example.com"
    exit 1
else
    echo "Firewall verification passed - unable to reach https://example.com as expected"
fi

if ! curl --connect-timeout 5 https://api.github.com/zen >/dev/null 2>&1; then
    echo "ERROR: Firewall verification failed - unable to reach https://api.github.com"
    exit 1
else
    echo "Firewall verification passed - able to reach https://api.github.com as expected"
fi

if ! curl --connect-timeout 5 https://registry.npmjs.org >/dev/null 2>&1; then
    echo "ERROR: dnsmasq ipset mirroring not working - registry.npmjs.org unreachable"
    exit 1
else
    echo "Firewall verification passed - dnsmasq zone (registry.npmjs.org) reachable"
fi
