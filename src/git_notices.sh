#!/bin/bash
# src/git_notices.sh — host-side up-time notices, sourced by up.sh and
# unit-tested by tests/bash.test.sh. NOT baked into the image (runs on the
# host, unlike wire_plugins.py). Extracted from up.sh so the real notice
# logic is executable in tests, not just mirrored (same precedent as
# src/keyfiles.sh).
#
# Both functions print the same `  note: …` lines up.sh always printed and
# return 0 — neither is ever fatal; a missing token or a missing egress zone
# fails at the FIRST clone, not at bring-up. These just warn early.

# git_owner_notices <REPOS> <GIT_ORG_TOKENS>
# Up-time notice: an owner routed via a non-github repos: URL, with no
# git.orgs token for THAT OWNER, will fail every private clone of its repos —
# there is no fall-back to human credentials (docs/secrets.md). Per owner, not
# per host: a host with one bound owner would otherwise hide a sibling owner
# on that same host with no token — the old per-host notice missed that.
# github.com is skipped (below) because its GH_TOKEN/gh fall-backs mean a
# clone there never fails for lack of a per-org token, so it needs no notice.
# Warn now, not at the first failed clone. bash-3.2 compatible: while-read
# over heredocs, no process substitution. _seen tracks host/owner pairs
# already reported (bash 3.2 has no associative arrays) so a host/owner with
# several repos: entries gets one notice, not one per repo.
git_owner_notices() {
    local repos="$1" git_org_tokens="$2"
    local _seen="" _rname _rurl _rscheme _rhost _rpath _rowner _rkey _rcanon \
        _org_has_token _org_owner _org_canon _org_src
    _seen=""
    while IFS=$'\t' read -r _rname _rurl; do
        [ -n "$_rname" ] || continue
        _rscheme=$(printf '%s' "${_rurl%%://*}" | tr '[:upper:]' '[:lower:]')
        [ "$_rscheme" = https ] || continue   # only https:// repos get the router; scp-style and ssh:// never need a token here
        # Host and owner, derived exactly like the clone block below: cut the
        # path off first, then drop userinfo — a `@` inside the path must not
        # read as userinfo.
        _rhost="${_rurl#*://}"; _rpath="${_rhost#*/}"; _rhost="${_rhost%%/*}"; _rhost="${_rhost##*@}"
        _rhost=$(printf '%s' "$_rhost" | tr '[:upper:]' '[:lower:]')   # hostnames are case-insensitive
        _rhost="${_rhost%:443}"
        [ "$_rhost" = github.com ] && continue   # github.com always has the default GH_TOKEN/gh fall-backs
        _rowner="${_rpath%%/*}"
        _rowner=$(printf '%s' "$_rowner" | tr '[:upper:]' '[:lower:]')   # case-fold to match GIT_ORG_TOKENS
        _rkey="$_rhost/$_rowner"   # quoted in the case pattern below, so it is matched literally — never sanitise it: a.b and a_b are different owners
        case " $_seen " in *" $_rkey "*) continue ;; esac
        _seen="$_seen $_rkey"
        _rcanon="GH_TOKEN_${_rowner//[!a-z0-9]/_}"                      # parity with _canonical_token_var
        _org_has_token=""
        while IFS=$'\t' read -r _org_owner _org_canon _org_src; do
            [ -n "$_org_owner" ] || continue
            [ "$_org_canon" = "$_rcanon" ] && _org_has_token=1
        done <<EOF
$git_org_tokens
EOF
        if [ -z "$_org_has_token" ]; then
            echo "  note: $_rhost/$_rowner: no git.orgs token for this owner — private clones of its repos will fail (no fall-back to human credentials; see docs/secrets.md)"
        fi
    done <<EOF
$repos
EOF
    return 0
}

# git_egress_notices <GIT_CREDENTIAL_HOSTS> <EGRESS> <EGRESS_CIDRS>
# Egress notice: a bound non-github host — a repos: origin, or a host bound
# only via git.orgs.<owner>.host: with no repos: entry of its own (which the
# per-owner loop above never even sees) — is never auto-allowlisted in the
# container's firewall. capabilities.egress must name it, or a parent domain
# of it (a zone covers its subdomains), or the router's own clones are
# refused at the firewall before git ever gets a chance to answer with a
# credential. GIT_CREDENTIAL_HOSTS (manifest.py) is the union of both: every
# repos:-derived non-github host plus every git.orgs-bound host, one
# https://host[:port] per line. Warn now, not at the first refused clone.
# bash-3.2 compatible: while-read over a heredoc, no process substitution.
git_egress_notices() {
    local git_credential_hosts="$1" egress="$2" egress_cidrs="$3"
    local _ehost _ok _d _is_ip _ip_re='^[0-9]+(\.[0-9]+){3}$'
    egress=$(printf '%s' "$egress" | tr '[:upper:]' '[:lower:]')   # zone list is matched case-insensitively
    while IFS= read -r _ehost; do
        [ -n "$_ehost" ] || continue
        _ehost="${_ehost#*://}"; _ehost="${_ehost%%/*}"; _ehost="${_ehost%:*}"
        _ehost=$(printf '%s' "$_ehost" | tr '[:upper:]' '[:lower:]')
        # An IP-literal host has no DNS name, so no zone in
        # capabilities.egress could ever cover it — that's what
        # capabilities.egress_cidrs (EGRESS_CIDRS) is for. A non-empty
        # EGRESS_CIDRS means a CIDR grant may already reach this host in a way
        # the domain zone list can never express, so skip the notice rather
        # than warning on every IP-literal git host that's actually fine.
        _is_ip=""
        [[ "$_ehost" =~ $_ip_re ]] && _is_ip=1
        if [ -n "$_is_ip" ] && [ -n "$egress_cidrs" ]; then
            continue
        fi
        _ok=""
        _d="$_ehost"
        while [ -n "$_d" ]; do
            case ",$egress," in *",$_d,"*) _ok=1; break;; esac
            case "$_d" in *.*) _d="${_d#*.}";; *) _d="";; esac
        done
        if [ -z "$_ok" ]; then
            if [ -z "$_is_ip" ] && [ -n "$egress_cidrs" ]; then
                # A DNS-named LAN host may be covered by a CIDR grant that this
                # zone walk cannot see — soften the wording rather than the
                # false-positive "will be refused" of the plain note. Named by
                # the address it resolves to, not the CIDR "covering" the
                # name itself — a private CIDR grant can never cover a public
                # DNS name as such, only the address that name resolves to.
                echo "  note: $_ehost: git host is not in capabilities.egress — clones will be refused by the firewall unless it was allowed another way (a saved allow-egress domain, or egress_cidrs ($egress_cidrs) covering the address it resolves to)"
            else
                echo "  note: $_ehost: git host is not in capabilities.egress — clones will be refused by the firewall unless it was allowed another way (a saved allow-egress domain)"
            fi
        fi
    done <<EOF
$git_credential_hosts
EOF
    return 0
}
