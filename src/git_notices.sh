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

# git_url_split <url>
# Host and path from a repo URL, exactly as up.sh's clone loop derived them
# before this was extracted: https://[user@]host[:port]/owner/repo and
# scp-style [user@]host:owner/repo. Cut the path off FIRST, then drop
# userinfo — a `@` inside the path must not read as userinfo, and a `*` in a
# case pattern crosses `/`, so no URL globs. Sets the caller-visible _h (host,
# lowercased — tr, not ${_h,,}: macOS bash 3.2) and _p (path after the host);
# ${_p%%/*} is the owner. bash 3.2 has no local-with-nameref, so these are
# plain globals like the rest of this file's caller-visible outputs.
git_url_split() {
    local url="$1"
    case "$url" in
        *://*) _h="${url#*://}"; _p="${_h#*/}"; _h="${_h%%/*}"; _h="${_h##*@}" ;;
        *)     _h="${url%%:*}"; _p="${url#*:}"; _h="${_h##*@}" ;;
    esac
    _h=$(printf '%s' "$_h" | tr '[:upper:]' '[:lower:]')   # hostnames are case-insensitive (tr, not ${_h,,}: macOS bash 3.2)
}

# git_host_notices <REPOS> <GIT_HOST_TOKENS>
# Up-time notice: an https:// repos: host with NO row in the git.hosts table
# has no credential at all — private clones of its repos will fail, with no
# fall-back to human credentials (docs/secrets.md). Every host the table names
# carries a token, and the CLI host always carries a row (manifest.py adds the
# implicit default when the manifest declares none), so the notice only ever
# fires for a repo host the manifest never declared a token for. Warn now, not
# at the first failed clone. bash-3.2 compatible: while-read over heredocs, no
# process substitution, no associative arrays. _seen tracks hosts already
# reported, so a host with several repos: entries gets one notice, not one per
# repo.
git_host_notices() {
    local repos="$1" git_host_tokens="$2"
    local _seen="" _rname _rurl _rscheme _rhost _pair _rowhost _has
    _seen=""
    while IFS=$'\t' read -r _rname _rurl; do
        [ -n "$_rname" ] || continue
        _rscheme=$(printf '%s' "${_rurl%%://*}" | tr '[:upper:]' '[:lower:]')
        [ "$_rscheme" = https ] || continue   # only https:// repos get the router; scp-style and ssh:// never need a token here
        # Host, derived via git_url_split, the same split the clone loop uses
        # (up.sh) — it already lowercases the host and drops userinfo, so only
        # the :443 default port is stripped here (the same normalisation as
        # the table rows below).
        git_url_split "$_rurl"; _rhost="${_h%:443}"
        case " $_seen " in *" $_rhost "*) continue ;; esac
        _seen="$_seen $_rhost"
        _has=""
        for _pair in $git_host_tokens; do
            case "$_pair" in *=*) ;; *) continue ;; esac
            _rowhost=${_pair%%=*}
            _rowhost=$(printf '%s' "$_rowhost" | tr '[:upper:]' '[:lower:]')
            _rowhost=${_rowhost%:443}
            [ "$_rowhost" = "$_rhost" ] && { _has=1; break; }
        done
        if [ -z "$_has" ]; then
            echo "  note: $_rhost: no git.hosts.$_rhost.token — private clones of its repos will fail (no fall-back to human credentials; see docs/secrets.md)"
        fi
    done <<EOF
$repos
EOF
    return 0
}

# git_orgs_host_notice <REPOS> <GIT_HOST_TOKENS> <GIT_ORG_IDENTITIES>
# Up-time notice: a git.orgs entry supplies its host's table row, so that one
# token now serves EVERY owner whose https:// repos: URLs name the host — one
# host carries one token. When a host has another owner besides the git.orgs
# one, print one line naming git.hosts.<host>.token as the explicit way to
# state that scope. bash-3.2 compatible: while-read over heredocs, no
# associative arrays, no ${var,,}.
git_orgs_host_notice() {
    local repos="$1" git_host_tokens="$2" git_org_identities="$3"
    local _rname _rurl _rscheme _h _host _owner _key _hosts_seen="" _owners         _count _pair _rowhost _rowvar
    [ -n "$git_org_identities" ] || return 0   # no git.orgs entries: nothing to state
    # Distinct https hosts in repos: order.
    while IFS=$'\t' read -r _rname _rurl; do
        [ -n "$_rname" ] || continue
        _rscheme=$(printf '%s' "${_rurl%%://*}" | tr '[:upper:]' '[:lower:]')
        [ "$_rscheme" = https ] || continue
        git_url_split "$_rurl"; _host="${_h%:443}"
        case " $_hosts_seen " in *" $_host "*) continue ;; esac
        _hosts_seen="$_hosts_seen $_host"
    done <<EOF
$repos
EOF
    for _host in $_hosts_seen; do
        # Distinct owners of this host; >1 means the row serves them all.
        _owners=" "; _count=0
        while IFS=$'\t' read -r _rname _rurl; do
            [ -n "$_rname" ] || continue
            _rscheme=$(printf '%s' "${_rurl%%://*}" | tr '[:upper:]' '[:lower:]')
            [ "$_rscheme" = https ] || continue
            git_url_split "$_rurl"
            [ "${_h%:443}" = "$_host" ] || continue
            _owner="${_p%%/*}"
            _owner=$(printf '%s' "$_owner" | tr '[:upper:]' '[:lower:]')   # case-fold: the attribution matches the clone URL's owner
            _key="$_host/$_owner"   # quoted in the case pattern below, so it is matched literally — never sanitise it
            case "$_owners" in *" $_key "*) continue ;; esac
            _owners="$_owners$_key "
            _count=$((_count + 1))
        done <<EOF
$repos
EOF
        [ "$_count" -gt 1 ] || continue
        _rowvar=""
        for _pair in $git_host_tokens; do
            case "$_pair" in *=*) ;; *) continue ;; esac
            _rowhost=${_pair%%=*}
            _rowhost=$(printf '%s' "$_rowhost" | tr '[:upper:]' '[:lower:]')
            _rowhost=${_rowhost%:443}
            if [ "$_rowhost" = "$_host" ]; then _rowvar=${_pair#*=}; break; fi
        done
        [ -n "$_rowvar" ] || continue
        echo "  note: $_host: $_rowvar now serves every owner on this host (a git.orgs entry supplies this row) — one host carries one token; state it explicitly with git.hosts.$_host.token: $_rowvar"
    done
    return 0
}

# git_egress_notices <GIT_CREDENTIAL_HOSTS> <EGRESS> <EGRESS_CIDRS>
# Egress notice: a host in GIT_CREDENTIAL_HOSTS — a repos: origin, or a host
# the git.hosts table named with no repos: entry of its own (which the per-host
# loop above never even sees) — is never auto-allowlisted in the container's
# firewall. capabilities.egress must name it, or a parent domain of it (a zone
# covers its subdomains), or the router's own clones are
# refused at the firewall before git ever gets a chance to answer with a
# credential. GIT_CREDENTIAL_HOSTS (manifest.py) is the union of both: every
# repos:-derived https:// host plus every git.hosts table host, one
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
        # capabilities.egress_cidrs (EGRESS_CIDRS) is for, and it is never in
        # a DNS zone list either way, so skip the zone walk below for it. A
        # non-empty EGRESS_CIDRS means a CIDR grant MAY already reach this
        # host in a way the domain zone list can never express — "may", not
        # "does": nothing here parses the CIDRs to check containment, so
        # soften the wording the same way the DNS-named branch below does,
        # rather than staying silent on an IP-literal host no grant actually
        # covers. Empty EGRESS_CIDRS keeps the plain wording (no grant of any
        # kind could apply), by falling through to the zone walk unchanged.
        _is_ip=""
        [[ "$_ehost" =~ $_ip_re ]] && _is_ip=1
        if [ -n "$_is_ip" ] && [ -n "$egress_cidrs" ]; then
            echo "  note: $_ehost: git host is not in capabilities.egress — clones will be refused by the firewall unless egress_cidrs ($egress_cidrs) covers it"
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
