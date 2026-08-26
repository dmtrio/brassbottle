#!/bin/bash
# egress_broker_firewall.sh — iptables REDIRECT + companion filter for the
# in-container transparent egress broker (B3). Sourced by init-firewall.sh;
# also invoked by the broker supervisor on startup failure / process death.
set -euo pipefail

BROKER_REDIRECT_PORT="${BROKER_REDIRECT_PORT:-3128}"

# shellcheck disable=SC2128
egress_broker_firewall_nat_rule() {
    echo -t nat -A OUTPUT -p tcp -m multiport --dports 80,443 \
        -m set ! --match-set allowed-domains dst \
        -m owner ! --uid-owner djinnbroker \
        -j REDIRECT --to-ports "$BROKER_REDIRECT_PORT"
}

egress_broker_firewall_filter_rule() {
    echo -A OUTPUT -p tcp -d 127.0.0.1 --dport "$BROKER_REDIRECT_PORT" -j ACCEPT
}

egress_broker_firewall_add() {
    # shellcheck disable=SC2046
    iptables $(egress_broker_firewall_nat_rule)
    # shellcheck disable=SC2046
    iptables $(egress_broker_firewall_filter_rule)
}

egress_broker_firewall_remove() {
    # shellcheck disable=SC2046
    iptables -t nat -D OUTPUT -p tcp -m multiport --dports 80,443 \
        -m set ! --match-set allowed-domains dst \
        -m owner ! --uid-owner djinnbroker \
        -j REDIRECT --to-ports "$BROKER_REDIRECT_PORT" 2>/dev/null || true
    iptables -D OUTPUT -p tcp -d 127.0.0.1 --dport "$BROKER_REDIRECT_PORT" \
        -j ACCEPT 2>/dev/null || true
}

egress_broker_firewall_dry_run() {
    echo "iptables $(egress_broker_firewall_nat_rule)"
    echo "iptables $(egress_broker_firewall_filter_rule)"
}

case "${1:-}" in
    add) egress_broker_firewall_add ;;
    remove) egress_broker_firewall_remove ;;
    dry-run) egress_broker_firewall_dry_run ;;
    *)
        echo "Usage: $0 {add|remove|dry-run}" >&2
        exit 1
        ;;
esac
