#!/bin/bash
# src/keyfiles.sh — host-side key-file composition, sourced by up.sh and
# unit-tested by tests/bash.test.sh. NOT baked into the image (runs on the host,
# unlike wire_plugins.py). Extracted from up.sh so the real composition logic is
# executable in tests, not just mirrored.
#
# Writes ONE COMPLETE env file per shim agent. Plugin credentials arrive as
# already-resolved per-agent records (common defaults, overrides, and disables
# were handled by manifest.py), so `cat <agent>.env` is the full audit of what
# that agent sees.
#
# The VALUES come from the current environment via indirect expansion
# (${!source}); the caller sources secrets.env. This file, like the Python
# modules, only ever handles NAMES in its arguments — never values.

warn_missing() { echo "  ⚠ $1 not in secrets.env — $2 will not authenticate until set"; }

# warn_unbound_org_token <file> <VAR> — used by bin/update-agent-keys.sh right
# after it hand-sets a per-org token (GH_TOKEN_<owner>) in one agent's env
# file. A per-org token needs its host binding (GH_HOST_<owner>=<host>) in the
# SAME file, or git-credential-org.sh refuses to present it (see
# src/git-credential-org.sh) — up.sh always writes both together from the
# manifest, but a hand-set override via update-agent-keys.sh only knows the
# VAR it was given, never the host, so it can't write the binding itself.
# No-op for any VAR that isn't a GH_TOKEN_* (every other kind of key has no
# such binding to check), and for any GH_TOKEN_* whose suffix isn't a
# canonical per-org owner (all-lowercase alphanumerics/underscores, matching
# _canonical_token_var's output) — that excludes GH_TOKEN_VARS (the scan list
# itself, not a token) and any GH_TOKEN_<Name>-shaped source var a plugin
# might define with its own casing, neither of which has a GH_HOST_ binding
# to check. bash-3.2 compatible.
warn_unbound_org_token() {
    local file="$1" var="$2" hostvar
    case "$var" in
        GH_TOKEN_*)
            case "${var#GH_TOKEN_}" in *[!a-z0-9_]*) return 0 ;; esac
            hostvar="GH_HOST_${var#GH_TOKEN_}"
            grep -q "^$hostvar=" "$file" \
                || echo "  ⚠ $var set without a matching GH_HOST_<owner> in $(basename "$file") — git-credential-org reads GH_HOST_<owner> beside the canonical GH_TOKEN_<owner> (owner lowercased, non-alphanumerics as _); set it the same way (github.com for a github org)" >&2
            ;;
    esac
}

# note_orphan_org_binding <file> <VAR> — used by bin/update-agent-keys.sh right
# after it REMOVES a per-org token (GH_TOKEN_<owner>) from one agent's env
# file. If that file still carries the owner's host binding
# (GH_HOST_<owner>=…), the binding now points nowhere — git-credential-org
# just never sees a GH_TOKEN_<owner> to route, but the stale GH_HOST_<owner>
# line sits there looking live. Sibling of warn_unbound_org_token, same
# canonical-suffix filter (no-op for a non-GH_TOKEN_* var, GH_TOKEN_VARS
# itself, or a non-canonical/mixed-case suffix — none of those has a
# GH_HOST_ binding to check). bash-3.2 compatible.
note_orphan_org_binding() {
    local file="$1" var="$2" hostvar
    case "$var" in
        GH_TOKEN_*)
            case "${var#GH_TOKEN_}" in *[!a-z0-9_]*) return 0 ;; esac
            hostvar="GH_HOST_${var#GH_TOKEN_}"
            # if/fi, not `grep && echo`: under set -e (this runs from
            # bin/update-agent-keys.sh) a bare `&&` whose left side is the
            # common case — no binding present, nothing to note — would
            # return 1 as the function's own status and abort the caller,
            # the same footgun tests/bash.test.sh pins for
            # warn_unbound_org_token's OLD call site (grep 'Regression:' there).
            if grep -q "^$hostvar=" "$file"; then
                echo "  note: $hostvar still set in $file — remove it the same way if the token is gone for good" >&2
            fi
            ;;
    esac
    return 0
}

# write_keyfiles <keys_dir> <shim_agents> <plugin_env_secrets> <agent_secrets> [<git_org_tokens>] [<git_org_hosts>]
#   keys_dir            already exists, mode 700, wiped of *.env by the caller
#   shim_agents         space-separated agent names (match the Dockerfile shims)
#   plugin_env_secrets  legacy shared passthrough records (currently empty)
#   agent_secrets       AGENT<TAB>SLOT<TAB>SOURCE resolved records (manifest.py)
#   git_org_tokens      OWNER<TAB>CANONICAL<TAB>SOURCE per line (per-org, from manifest.py)
#   git_org_hosts       OWNER<TAB>HOSTVAR<TAB>HOST per line (per-org, from manifest.py)
# Reads GH_TOKEN and every SOURCE var from the environment (indirect expansion).
write_keyfiles() {
    local keys_dir="$1" shim_agents="$2" plugin_env_secrets="$3" agent_secrets="$4" git_org_tokens="${5:-}" git_org_hosts="${6:-}"
    local shared="" slot src hint agent a f owner canonical hostvar host

    # Shared block: legacy passthroughs + GH_TOKEN, built once. The
    # heredoc keeps the loop in this shell so the warns aren't lost to a pipe
    # subshell.
    while IFS=$'\t' read -r slot src hint; do
        [ -n "$slot" ] || continue
        if [ -n "${!src:-}" ]; then
            shared="${shared}${slot}=${!src}"$'\n'
        else
            warn_missing "$src" "$hint"
        fi
    done <<EOF
$plugin_env_secrets
EOF
    [ -n "${GH_TOKEN:-}" ] && shared="${shared}GH_TOKEN=$GH_TOKEN"$'\n'

    # Per-org tokens sit in the shared block alongside GH_TOKEN, one canonical
    # GH_TOKEN_<owner>=<value> per line, so git-credential-org can route by
    # owner. No warn_missing: a per-org source var absent from secrets.env is a
    # hard error in manifest.py (like agent_secrets), so it can't reach here.
    while IFS=$'\t' read -r owner canonical src; do
        [ -n "$owner" ] || continue
        shared="${shared}${canonical}=${!src}"$'\n'
    done <<EOF
$git_org_tokens
EOF

    # The host each per-org token is bound to, written beside it as
    # GH_HOST_<owner>=<host> — so git-credential-org can refuse to present the
    # token to any other host. A host is not a secret; it rides in the same
    # keyfile as the token it's paired with for convenience, not confidentiality.
    while IFS=$'\t' read -r owner hostvar host; do
        [ -n "$owner" ] || continue
        shared="${shared}${hostvar}=${host}"$'\n'
    done <<EOF
$git_org_hosts
EOF

    # Fan the shared block out to every shim agent. chmod 600 as each file is
    # created — it already holds secret values, so don't leave it at the umask
    # default even for the window until the trailing chmod.
    for a in $shim_agents; do
        printf '%s' "$shared" > "$keys_dir/$a.env"; chmod 600 "$keys_dir/$a.env"
    done

    # Append each agent's resolved plugin secrets. No warn_missing here: an
    # override source is validated by manifest.py, while an unset common source
    # is omitted before it becomes a resolved record.
    while IFS=$'\t' read -r agent slot src; do
        [ -n "$agent" ] || continue
        echo "$slot=${!src}" >> "$keys_dir/$agent.env"
    done <<EOF
$agent_secrets
EOF

    for f in "$keys_dir"/*.env; do [ -f "$f" ] && chmod 600 "$f"; done
}
