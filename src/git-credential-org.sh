#!/bin/bash
# git-credential-org — route HTTPS git credentials by host.
#
# git invokes a credential helper as `<helper> get` with the request (protocol,
# host, and — because we set credential.useHttpPath=true — path) on stdin. The
# manifest's git.hosts table maps each host to the secrets.env variable that
# holds its token; manifest.py derives GIT_HOST_TOKENS as space-separated
# host=VARIABLE pairs, and keyfiles.sh writes the table plus every named
# variable into each agent env file (and up.sh forwards both into the
# bootstrap clone). A request whose host is listed returns that variable's
# value by indirect expansion — the token is presented to no other host.
#
# Host normalisation MUST match manifest.py's own (_normalize_git_host):
# lowercased, a trailing :443 (the explicit default port git passes for an
# https URL spelled with it) stripped. A mismatch silently fails to match a
# row — the exact bug this feature exists to prevent.
#
# An unlisted host defers to `gh auth git-credential` — but ONLY to a STORED
# gh login for EXACTLY this host, never to anything else. gh normalises
# *.github.com to github.com and honours GH_TOKEN/GITHUB_TOKEN (and their
# _ENTERPRISE variants) from its environment, so letting gh decide would
# present a token to a host the table never named. Two locks:
#   • "gh holds a login" is an exact, fixed-string match of the host as a
#     top-level key in gh's own hosts file
#     (${GH_CONFIG_DIR:-${XDG_CONFIG_HOME:-$HOME/.config}/gh}/hosts.yml) —
#     gh's own normalisation never widens it;
#   • when deferring, the four token variables are stripped from gh's
#     environment, so only the stored login can answer.
# No stored login for this host (or a deferral that fails or returns no
# password) → quit=1 with a stderr line naming git.hosts.<host>.token, so
# git fails immediately instead of prompting. A listed host whose variable
# is unset (or empty) takes the same path.
#
# No host is special-cased here: every host resolves through the one
# GIT_HOST_TOKENS walk below.

[ "$1" = get ] || exit 0                 # store/erase: no-op (stateless helper)

req=$(cat)                                # buffer the request so gh can replay it
host=$(printf '%s\n' "$req" | sed -n 's/^host=//p' | head -n 1)   # the FIRST host= line only — a second one must not widen the lookup
host=$(printf '%s' "$host" | tr '[:upper:]' '[:lower:]')   # hostnames are case-insensitive; git passes the URL's spelling
host=${host%:443}                        # explicit default port: git passes host=host.example:443 for https://host.example:443/…
[ -n "$host" ] || { echo "git-credential-org: request carries no host= line — nothing to route" >&2; echo "quit=1"; exit 0; }   # no host= line: nothing to route; stop git rather than fall through to another helper or a prompt

# Walk the table for this host. Unquoted $GIT_HOST_TOKENS word-splits the
# space-separated host=VARIABLE pairs; values are manifest-validated names
# (and the table value is derived, not user input), so no word carries
# whitespace or glob characters.
tok=""
table_var=""
for pair in ${GIT_HOST_TOKENS:-}; do
    case "$pair" in *=*) ;; *) continue ;; esac
    rowhost=${pair%%=*}
    rowhost=$(printf '%s' "$rowhost" | tr '[:upper:]' '[:lower:]')
    rowhost=${rowhost%:443}              # same normalisation as $host
    if [ "$rowhost" = "$host" ]; then
        table_var=${pair#*=}
        tok="${!table_var:-}"
        break
    fi
done

if [ -n "$tok" ]; then
    echo "username=x-access-token"
    echo "password=$tok"
else
    gh_hosts="${GH_CONFIG_DIR:-${XDG_CONFIG_HOME:-$HOME/.config}/gh}/hosts.yml"
    gh_out=""
    gh_rc=1
    if [ -f "$gh_hosts" ] && grep -Fxq "$host:" "$gh_hosts"; then
        # Stored gh login for exactly this host: defer, with every token
        # variable stripped so only that login can answer. A deferral that
        # fails (gh exits non-zero) or succeeds WITHOUT a password= line
        # (no usable token stored, e.g. a hosts.yml entry that only names a
        # protocol) must NOT fall through to a prompt — git gets the same
        # stderr line + quit=1 as an unlisted host.
        gh_out=$(printf '%s\n' "$req" | env -u GH_TOKEN -u GITHUB_TOKEN \
            -u GH_ENTERPRISE_TOKEN -u GITHUB_ENTERPRISE_TOKEN \
            gh auth git-credential get)   # human fallback
        gh_rc=$?
        if [ "$gh_rc" -eq 0 ] && printf '%s\n' "$gh_out" | grep -q '^password='; then
            printf '%s\n' "$gh_out"
            exit 0
        fi
    fi
    # No token for this host, and gh holds no usable login for it: say
    # exactly what is wrong and tell git to stop — no other helper, no
    # terminal prompt (which would hang an agent's clone waiting for a
    # username). git then fails with "credential helper … told us to
    # quit" plus this line on stderr.
    if [ -n "$table_var" ]; then
        echo "git-credential-org: git.hosts.$host.token: '$table_var' is not set in secrets.env — put the token there (git.hosts.$host.token names the variable)" >&2
    else
        echo "git-credential-org: no git.hosts.$host.token — add git.hosts.$host.token: <secrets.env var name> to the bottle and the token to secrets.env" >&2
    fi
    echo "quit=1"
fi