#!/bin/bash
# src/keyfiles.sh — host-side key-file composition, sourced by up.sh and
# unit-tested by tests/bash.test.sh. NOT baked into the image (runs on the host,
# unlike wire_plugins.py). Extracted from up.sh so the real composition logic is
# executable in tests, not just mirrored.
#
# Writes ONE COMPLETE env file per shim agent — and one for the `user`
# identity, the human's interactive shell (the image's .bashrc sources it).
# Plugin credentials arrive as already-resolved per-agent records (common
# defaults, overrides, and disables were handled by manifest.py), so
# `cat <agent>.env` is the full audit of what that agent sees.
#
# Git routing is PER IDENTITY: every env file carries only the rows that
# serve its identity — the catch-all/simple-form table (GIT_HOST_TOKENS:
# simple-form git.hosts entries, git.token, git.orgs, and a list-form
# host's catch-all entry) plus the rows of the list-form entries that name
# it (GIT_IDENTITY_HOST_TOKENS records), and GH_TOKEN only when ITS
# github.com row exists (its own row's variable, or the catch-all's). An
# identity no entry names and no catch-all covers gets no row for that
# host and no GH_TOKEN: one agent's env file never contains another
# entry's token.
#
# The VALUES come from the current environment via indirect expansion
# (${!source}); the caller sources secrets.env. This file, like the Python
# modules, only ever handles NAMES in its arguments — never values.

warn_missing() { echo "  ⚠ $1 not in secrets.env — $2 will not authenticate until set"; }

# git_host_token_pairs <git_host_tokens>
#   Emit one VAR=VALUE per line for a host=VARNAME table's credentials: the
#   table itself plus every variable it names, read from the environment by
#   indirect expansion. One walk serves BOTH consumers: write_keyfiles builds
#   each identity's git block from it, and up.sh reads it one line at a time
#   into the bootstrap clone's `docker exec -e "$line"` args (the clone exec
#   isn't shim-launched and runs as no identity, so it takes the
#   catch-all/simple-form rows only — GIT_HOST_TOKENS, never the
#   per-identity tables). Names are manifest-validated; values are secrets
#   and can be anything — but the pairing is line-delimited, so a value must
#   not contain a newline, or the VAR=VALUE pairs it splits into would be
#   mangled. Each line rides as ONE argv word. A variable whose value is
#   empty (the token was never set) is skipped: the helper then takes its
#   gh/quit path for that host, exactly as it would for an unset variable.
#   Deduped: one row var may serve several hosts.
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

# identity_table <ident> <git_host_tokens> <git_identity_host_tokens>
#   The one table the helper and the env file see for identity <ident>: the
#   rows of the list-form entries that name it (a GIT_IDENTITY_HOST_TOKENS
#   record: ident<TAB>host=VAR pairs), layered over the catch-all rows
#   (GIT_HOST_TOKENS) for every host NO entry names it for — a named row
#   OVERRIDES the catch-all for its host, since the entry that names the
#   identity is that host's token for it. Prints one space-separated
#   host=VARNAME string; the catch-all table unchanged when <ident> is named
#   by no entry. bash-3.2 compatible: while-read over a heredoc, no
#   associative arrays, no ${var,,}.
identity_table() {
    local ident="$1" git_host_tokens="$2" git_identity_host_tokens="$3"
    local named="" rid rrows pair host out
    while IFS=$'\t' read -r rid rrows; do
        [ "$rid" = "$ident" ] && named="$rrows"
    done <<EOF
$git_identity_host_tokens
EOF
    out="$named"
    for pair in $git_host_tokens; do
        case "$pair" in *=*) ;; *) continue ;; esac
        host=${pair%%=*}
        case " $named " in *" $host="*) ;; *) case "$out" in "") out="$pair" ;; *) out="$out $pair" ;; esac ;; esac
    done
    printf '%s' "$out"
}

# identity_source <ident> <git_token_source> <git_identity_token_sources>
#   The variable GH_TOKEN is written from for identity <ident>: the CLI
#   host's row variable of ITS OWN table (a GIT_IDENTITY_TOKEN_SOURCES
#   record: ident<TAB>VAR) when one of its entries names the CLI host, else
#   GIT_TOKEN_SOURCE (the catch-all's CLI row). Empty = no CLI-host row for
#   this identity anywhere: no GH_TOKEN line.
identity_source() {
    local ident="$1" git_token_source="$2" git_identity_token_sources="$3"
    local rid rvar
    while IFS=$'\t' read -r rid rvar; do
        [ "$rid" = "$ident" ] && { printf '%s' "$rvar"; return 0; }
    done <<EOF
$git_identity_token_sources
EOF
    printf '%s' "$git_token_source"
}

# git_env_block <pairs> <cli_source>
#   The git-routing lines of one identity's env file: GIT_HOST_TOKENS plus
#   every variable the pairs name (under its own name, values by indirect
#   expansion, deduped, empty values skipped — an unset source var is a hard
#   error in manifest.py, like agent_secrets, so it can't reach here), then
#   GH_TOKEN from <cli_source> — unless the walk already wrote GH_TOKEN (a
#   row naming GH_TOKEN itself), so GH_TOKEN lands exactly once. GH_TOKEN
#   itself is never read from the environment — a value in the caller's env
#   reaches a key file only when a table row names it.
#   Values are shell-quoted (%q): these lines are SOURCED (the shims, and
#   .bashrc's user.env piece), so the space-separated table and any secret
#   value must survive sourcing byte-identically — an unquoted
#   GIT_HOST_TOKENS=host=VAR host=VAR line is parsed as assignments plus a
#   bogus command and silently drops the table. (git_host_token_pairs above
#   stays verbatim: the bootstrap clone env rides as single docker-exec argv
#   words, never sourced.) bash-3.2 compatible.
git_env_block() {
    local pairs="$1" cli_source="$2" pair var val seen=" " gh_written=""
    [ -n "$pairs" ] || return 0
    printf 'GIT_HOST_TOKENS=%q\n' "$pairs"
    for pair in $pairs; do
        case "$pair" in *=*) ;; *) continue ;; esac
        var=${pair#*=}
        [ -n "$var" ] || continue
        case "$seen" in *" $var "*) continue ;; esac
        seen="$seen$var "
        val="${!var:-}"
        [ -n "$val" ] || continue
        printf '%s=%q\n' "$var" "$val"
        case "$var" in GH_TOKEN) gh_written=1 ;; esac
    done
    if [ -z "$gh_written" ] && [ -n "$cli_source" ]; then
        val="${!cli_source:-}"
        [ -n "$val" ] && printf 'GH_TOKEN=%q\n' "$val"
    fi
    return 0
}

# write_keyfiles <keys_dir> <shim_agents> <plugin_env_secrets> <agent_secrets> [<git_host_tokens> [<git_token_source> [<git_identity_host_tokens> [<git_identity_token_sources>]]]]
#   keys_dir            already exists, mode 700, wiped of *.env by the caller
#   shim_agents         space-separated agent names (match the Dockerfile shims)
#   plugin_env_secrets  legacy shared passthrough records (currently empty)
#   agent_secrets       AGENT<TAB>SLOT<TAB>SOURCE resolved records (manifest.py)
#   git_host_tokens     GIT_HOST_TOKENS — space-separated host=SOURCEVAR pairs,
#                       the catch-all/simple-form table (manifest.py); each
#                       SOURCEVAR is written under its own name beside the
#                       table itself, in EVERY identity's file
#   git_token_source    GIT_TOKEN_SOURCE — the catch-all's CLI host row
#                       variable (manifest.py); the plain GH_TOKEN is written
#                       from THIS variable for identities with no github.com
#                       row of their own, by indirect expansion, never from
#                       the calling environment: empty = no catch-all CLI row
#   git_identity_host_tokens  GIT_IDENTITY_HOST_TOKENS — identity<TAB>pairs
#                       records (manifest.py): the rows of the list-form
#                       entries naming each identity; a named row overrides
#                       the catch-all row for its host
#   git_identity_token_sources  GIT_IDENTITY_TOKEN_SOURCES — identity<TAB>VAR
#                       records (manifest.py): each identity's own CLI-host
#                       row variable, written as its plain GH_TOKEN
# Writes one file per shim agent AND user.env (the `user` identity, sourced
# by the image's .bashrc — git routing only, never plugin secrets). Reads
# every SOURCE var from the environment (indirect expansion).
write_keyfiles() {
    local keys_dir="$1" shim_agents="$2" plugin_env_secrets="$3" agent_secrets="$4" \
        git_host_tokens="${5:-}" git_token_source="${6:-}" \
        git_identity_host_tokens="${7:-}" git_identity_token_sources="${8:-}"
    local shared="" slot src hint agent a f line pairs cli_source

    # Shared block: legacy passthroughs. The heredoc keeps the loop in this
    # shell so the warns aren't lost to a pipe subshell.
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

    # Every shim agent's file: plugin slots + ITS OWN git block (its
    # identity's table, GH_TOKEN from its own CLI row). Written with a group
    # redirect (NOT a variable round-trip: $(…) strips the block's trailing
    # newline, and the plugin-secret append below would fuse onto the last
    # git line, corrupting GH_TOKEN and losing the slot). chmod 600 as each
    # file is created — it already holds secret values, so don't leave it at
    # the umask default even for the window until the trailing chmod.
    for a in $shim_agents; do
        pairs=$(identity_table "$a" "$git_host_tokens" "$git_identity_host_tokens")
        cli_source=$(identity_source "$a" "$git_token_source" "$git_identity_token_sources")
        { printf '%s' "$shared"; git_env_block "$pairs" "$cli_source"; } > "$keys_dir/$a.env"
        chmod 600 "$keys_dir/$a.env"
    done

    # user.env: the `user` identity (the human's interactive shell, sourced
    # by the image's .bashrc piece) — its own table + variables + GH_TOKEN
    # exactly like an agent's, but NO plugin secrets: those are agent-scoped
    # and never reached the human's shell before per-identity key files.
    pairs=$(identity_table user "$git_host_tokens" "$git_identity_host_tokens")
    cli_source=$(identity_source user "$git_token_source" "$git_identity_token_sources")
    { git_env_block "$pairs" "$cli_source"; } > "$keys_dir/user.env"
    chmod 600 "$keys_dir/user.env"

    # Append each agent's resolved plugin secrets. No warn_missing here: an
    # override source is validated by manifest.py, while an unset common source
    # is omitted before it becomes a resolved record. `user` never appears in
    # AGENT_SECRETS (identities validate against agent binaries), so the human
    # env file stays routing-only. Values are shell-quoted (%q) like the git
    # block: these lines are sourced, so a value with spaces, quotes or $
    # must round-trip byte-identically (an unquoted append runs the value's
    # words as commands).
    while IFS=$'\t' read -r agent slot src; do
        [ -n "$agent" ] || continue
        printf '%s=%q\n' "$slot" "${!src}" >> "$keys_dir/$agent.env"
    done <<EOF
$agent_secrets
EOF

    for f in "$keys_dir"/*.env; do [ -f "$f" ] && chmod 600 "$f"; done
}
