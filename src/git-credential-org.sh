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
# An unlisted host defers to `gh auth git-credential` when gh holds a login
# for that host (the human's interactive lane — one credential lane serves
# agents AND humans); otherwise it answers quit=1 with a stderr line naming
# git.hosts.<host>.token, so git fails immediately instead of prompting. A
# listed host whose variable is unset (or empty) takes the same gh/quit path:
# gh decides whether IT holds a login for that host, so the human login is
# offered to a host only when gh itself was authenticated there.
#
# No host is special-cased here: every host — the table's, github's, any
# forge's — resolves through the one GIT_HOST_TOKENS walk below.

[ "$1" = get ] || exit 0                 # store/erase: no-op (stateless helper)

req=$(cat)                                # buffer the request so gh can replay it
host=$(printf '%s\n' "$req" | sed -n 's/^host=//p')
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
    if gh auth token --hostname "$host" >/dev/null 2>&1; then
        printf '%s\n' "$req" | gh auth git-credential get   # human fallback
    else
        # No token for this host, and gh holds no login for it either: say
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
fi
