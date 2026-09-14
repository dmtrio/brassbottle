#!/bin/bash
# git-credential-org — route HTTPS git credentials by repo owner.
#
# git invokes a credential helper as `<helper> get` with the request (protocol,
# host, and — because we set credential.useHttpPath=true — path) on stdin. The
# first path segment is the forge owner. We return GH_TOKEN_<owner> if that var
# is set (per-org identity). For github.com only, we then fall back to the
# default GH_TOKEN (container identity), else defer to `gh auth git-credential`
# for the human's interactive login — so one credential lane serves agents
# (token by owner) AND humans (gh fallback).
#
# The <owner> → GH_TOKEN_<owner> sanitization MUST match
# manifest.py:_canonical_token_var byte-for-byte: lowercase the owner (github
# owners are case-insensitive; the owner here comes from the clone URL, whose
# case we don't control), then GH_TOKEN_ + every non-alphanumeric byte replaced
# by '_'. A mismatch silently mis-routes to the default token — the exact bug
# this feature exists to prevent.
#
# entrypoint.sh installs this helper for github.com AND every non-github origin
# in the manifest's repos: (gitea, self-hosted). Both fall-backs are gated on
# host=github.com: GH_TOKEN is the github machine user's token and must never be
# presented to a third-party server, and gh knows nothing about other hosts.
# A non-github owner with no GH_TOKEN_<owner> set answers quit=1 with a stderr
# line naming the missing var, so git fails immediately instead of prompting.
#
# Per-org tokens are host-bound via GH_HOST_<owner> (manifest.py:_org_hosts,
# written beside the token by keyfiles.sh): a request from any other host never
# sees them — an ad-hoc clone from a same-named owner on a different forge gets
# no credential instead of the wrong one. A token with NO binding at all (no
# GH_HOST_<owner> set — up.sh always writes one for every git.orgs token, so
# this means a hand-set token, e.g. via bin/update-agent-keys.sh, with no
# matching GH_HOST_<owner>) is refused everywhere, github.com included: an
# empty $bound never equals a real $host, so there is no host left for which
# it would be presented. A request with no host= line at all (git credential
# fill invoked by hand) has nothing to route either: answer quit=1 so git
# stops there instead of falling through to another helper or a prompt.

[ "$1" = get ] || exit 0                 # store/erase: no-op (stateless helper)

req=$(cat)                                # buffer the request so gh can replay it
host=$(printf '%s\n' "$req" | sed -n 's/^host=//p')
host=$(printf '%s' "$host" | tr '[:upper:]' '[:lower:]')   # hostnames are case-insensitive; git passes the URL's spelling
host=${host%:443}                        # explicit default port: git passes host=github.com:443 for https://github.com:443/…
[ -n "$host" ] || { echo "git-credential-org: request carries no host= line — nothing to route" >&2; echo "quit=1"; exit 0; }   # no host= line: nothing to route; stop git rather than fall through to another helper or a prompt
path=$(printf '%s\n' "$req" | sed -n 's/^path=//p')
owner=${path%%/*}
# case-fold (github owners are case-insensitive), then sanitize. tr, not
# ${owner,,}: this helper is also exercised on the host by the test suite, and
# macOS ships bash 3.2 where that expansion is a syntax error.
owner=$(printf '%s' "$owner" | tr '[:upper:]' '[:lower:]')
clean=${owner//[!a-z0-9]/_}              # parity with _canonical_token_var
var="GH_TOKEN_${clean}"
hostvar="GH_HOST_${clean}"                # parity with manifest.py:_org_hosts
bound="${!hostvar:-}"                     # no binding recorded → refuse everywhere
bound=$(printf '%s' "$bound" | tr '[:upper:]' '[:lower:]'); bound="${bound%:443}"   # same normalisation as $host — a hand-set GH_HOST_<owner> may be spelled either way
tok="${!var:-}"
if [ -n "$tok" ] && [ "$bound" != "$host" ]; then
    # The per-org token was issued for $bound (or, if $bound is empty, for no
    # host at all — an empty $bound never equals a real $host, so an unbound
    # token is refused everywhere, github.com included). Never present it to
    # another host — for github.com the default/gh fall-backs still apply below.
    # On github.com that fall-through would otherwise be silent (the quit
    # branch below never runs there) — say so on stderr before falling back,
    # or a wrong-host refusal looks exactly like an ordinary default-token
    # clone.
    if [ "$host" = github.com ]; then
        if [ -z "$bound" ]; then
            echo "git-credential-org: $var is set but has no GH_HOST_<owner> binding — not presenting it; falling back to the container default or the gh login for $host" >&2
        else
            echo "git-credential-org: $var is bound to $bound, not $host — not presenting it; falling back to the container default or the gh login for $host" >&2
        fi
    fi
    tok=""
fi
if [ -z "$tok" ] && [ "$host" = github.com ]; then
    tok="${GH_TOKEN:-}"                   # container default: github only
fi

if [ -n "$tok" ]; then
    echo "username=x-access-token"
    echo "password=$tok"
elif [ "$host" = github.com ]; then
    printf '%s\n' "$req" | gh auth git-credential get   # human fallback
else
    # Non-github host, no usable GH_TOKEN_<owner>: say exactly what is wrong and
    # tell git to stop — no other helper, no terminal prompt (which would hang
    # an agent's clone waiting for a username). git then fails with
    # "credential helper … told us to quit" plus this line on stderr.
    if [ -n "${!var:-}" ] && [ -z "$bound" ]; then
        echo "git-credential-org: $var is set but has no GH_HOST_<owner> binding — refusing to present it (up writes the binding; a hand-set token via bin/update-agent-keys.sh needs GH_HOST_${clean}=$host too)" >&2
    elif [ -n "${!var:-}" ]; then
        echo "git-credential-org: $var is bound to $bound, not $host — refusing to present it (add git.orgs.<owner>.token: $var for $host if this owner also lives there)" >&2
    else
        echo "git-credential-org: no $var set for owner '$owner' on $host — add git.orgs.<owner>.token: $var to the bottle and the token to secrets.env" >&2
    fi
    echo "quit=1"
fi
