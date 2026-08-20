#!/bin/bash
# git-credential-org — route github.com credentials by repo owner.
#
# git invokes a credential helper as `<helper> get` with the request (protocol,
# host, and — because we set credential.useHttpPath=true — path) on stdin. The
# first path segment is the forge owner. We return GH_TOKEN_<owner> if that var
# is set (per-org identity), else the default GH_TOKEN (container identity),
# else defer to `gh auth git-credential` for the human's interactive login. So
# one credential lane serves agents (token by owner) AND humans (gh fallback).
#
# The <owner> → GH_TOKEN_<owner> sanitization MUST match
# bottle.py:_canonical_token_var byte-for-byte: lowercase the owner (github
# owners are case-insensitive; the owner here comes from the clone URL, whose
# case we don't control), then GH_TOKEN_ + every non-alphanumeric byte replaced
# by '_'. A mismatch silently mis-routes to the default token — the exact bug
# this feature exists to prevent.
#
# Forge is github-only for now (entrypoint installs this helper only for
# https://github.com). gitea is a follow-up.

[ "$1" = get ] || exit 0                 # store/erase: no-op (stateless helper)

req=$(cat)                                # buffer the request so gh can replay it
path=$(printf '%s\n' "$req" | sed -n 's/^path=//p')
owner=${path%%/*}
# case-fold (github owners are case-insensitive), then sanitize. tr, not
# ${owner,,}: this helper is also exercised on the host by the test suite, and
# macOS ships bash 3.2 where that expansion is a syntax error.
owner=$(printf '%s' "$owner" | tr '[:upper:]' '[:lower:]')
clean=${owner//[!a-z0-9]/_}              # parity with _canonical_token_var
var="GH_TOKEN_${clean}"
tok="${!var:-${GH_TOKEN:-}}"

if [ -n "$tok" ]; then
    echo "username=x-access-token"
    echo "password=$tok"
else
    printf '%s\n' "$req" | gh auth git-credential get   # human fallback
fi
