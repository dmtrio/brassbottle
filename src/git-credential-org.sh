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
# A non-github owner with no GH_TOKEN_<owner> set gets NO credential (git then
# fails 401, loudly) rather than the wrong one.

[ "$1" = get ] || exit 0                 # store/erase: no-op (stateless helper)

req=$(cat)                                # buffer the request so gh can replay it
host=$(printf '%s\n' "$req" | sed -n 's/^host=//p')
path=$(printf '%s\n' "$req" | sed -n 's/^path=//p')
owner=${path%%/*}
# case-fold (github owners are case-insensitive), then sanitize. tr, not
# ${owner,,}: this helper is also exercised on the host by the test suite, and
# macOS ships bash 3.2 where that expansion is a syntax error.
owner=$(printf '%s' "$owner" | tr '[:upper:]' '[:lower:]')
clean=${owner//[!a-z0-9]/_}              # parity with _canonical_token_var
var="GH_TOKEN_${clean}"
tok="${!var:-}"
if [ -z "$tok" ] && [ "$host" = github.com ]; then
    tok="${GH_TOKEN:-}"                   # container default: github only
fi

if [ -n "$tok" ]; then
    echo "username=x-access-token"
    echo "password=$tok"
elif [ "$host" = github.com ]; then
    printf '%s\n' "$req" | gh auth git-credential get   # human fallback
fi
