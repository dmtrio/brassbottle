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

# git_host_token_pairs <git_host_tokens>
#   Emit one VAR=VALUE per line for the git.hosts routing table's
#   credentials: GIT_HOST_TOKENS itself (the manifest's host→variable table,
#   from manifest.py) plus every variable it names, read from the environment
#   by indirect expansion. One walk serves BOTH consumers: write_keyfiles
#   builds the agent env files' shared block from it, and up.sh reads it one
#   line at a time into the bootstrap clone's `docker exec -e "$line"` args
#   (the clone exec isn't shim-launched, so without it the in-container
#   git-credential-org would find no table and no token and a private clone
#   would fail). Names are manifest-validated; values are secrets and can be
#   anything — but the pairing is line-delimited, so a value must not contain
#   a newline, or the VAR=VALUE pairs it splits into would be mangled. Each
#   line rides as ONE argv word. A variable whose
#   value is empty (the token was never set) is skipped: the helper then
#   takes its gh/quit path for that host, exactly as it would for an unset
#   variable. Deduped: one row var may serve several hosts.
#   bash-3.2 compatible: no associative arrays, no ${var,,}.
git_host_token_pairs() {
    local git_host_tokens="$1" pair var val seen=" "
    [ -n "$git_host_tokens" ] || return 0
    printf 'GIT_HOST_TOKENS=%s\n' "$git_host_tokens"
    for pair in $git_host_tokens; do
        case "$pair" in *=*) ;; *) continue ;; esac
        var=${pair#*=}
        [ -n "$var" ] || continue
        case "$seen" in *" $var "*) continue ;; esac
        seen="$seen$var "
        val="${!var:-}"
        [ -n "$val" ] || continue
        printf '%s=%s\n' "$var" "$val"
    done
    return 0
}

# write_keyfiles <keys_dir> <shim_agents> <plugin_env_secrets> <agent_secrets> [<git_host_tokens>]
#   keys_dir            already exists, mode 700, wiped of *.env by the caller
#   shim_agents         space-separated agent names (match the Dockerfile shims)
#   plugin_env_secrets  legacy shared passthrough records (currently empty)
#   agent_secrets       AGENT<TAB>SLOT<TAB>SOURCE resolved records (manifest.py)
#   git_host_tokens     GIT_HOST_TOKENS — space-separated host=SOURCEVAR pairs
#                       (manifest.py); each SOURCEVAR is written under its own
#                       name beside the table itself
# Reads GH_TOKEN and every SOURCE var from the environment (indirect expansion).
write_keyfiles() {
    local keys_dir="$1" shim_agents="$2" plugin_env_secrets="$3" agent_secrets="$4" git_host_tokens="${5:-}"
    local shared="" slot src hint agent a f line gh_written

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

    # The git.hosts routing table rides in the shared block beside the token
    # variables it names (a host is not a secret; it is here for convenience,
    # not confidentiality) — git-credential-org resolves each request's host
    # through it and reads the row variable by indirect expansion. Same walk
    # the bootstrap clone env uses (git_host_token_pairs); no warn_missing:
    # an unset source var is a hard error in manifest.py (like agent_secrets),
    # so it can't reach here. The row variables carry GH_TOKEN whenever the
    # CLI host's row names it — the default export below is then redundant and
    # is skipped, so GH_TOKEN lands exactly once.
    gh_written=""
    if [ -n "$git_host_tokens" ]; then
        while IFS= read -r line; do
            [ -n "$line" ] || continue
            shared="${shared}${line}"$'\n'
            case "$line" in GH_TOKEN=*) gh_written=1 ;; esac
        done <<EOF
$(git_host_token_pairs "$git_host_tokens")
EOF
    fi
    if [ -z "$gh_written" ] && [ -n "${GH_TOKEN:-}" ]; then
        shared="${shared}GH_TOKEN=$GH_TOKEN"$'\n'
    fi

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
