#!/bin/bash
# src/git_identity.sh — host-side per-repo author attribution, sourced by up.sh
# and unit-tested by tests/bash.test.sh. NOT baked into the image (runs on the
# host, unlike wire_plugins.py). Extracted from up.sh so the real "which author
# applies to this repo URL, and stamp it" decision is executable in tests, not
# just mirrored (same precedent as src/git_notices.sh / src/keyfiles.sh).
#
# Two tables feed it, both derived by src/manifest.py, and they never coexist
# in one manifest (manifest.py rejects git.hosts beside git.token/git.orgs):
#   GIT_ORG_IDENTITIES  owner<TAB>name<TAB>email per line (git.orgs spelling)
#   GIT_HOST_IDENTITIES host<TAB>name<TAB>email per line (git.hosts spelling)
# so there is no precedence question between the two lookups: whichever table
# is non-empty is the only one that can match.

# git_identity_for <owner> <host>
# The author that applies to a repo at <owner>/<repo> on <host>: the per-owner
# GIT_ORG_IDENTITIES record for the lowercased owner first, else the per-host
# GIT_HOST_IDENTITIES record for the normalised host. Prints "name<TAB>email";
# empty when neither table matches. Owner and host are lowercased here (tr,
# not ${var,,}: up.sh runs on the host, and macOS ships bash 3.2 where that
# expansion is a syntax error); the caller passes the raw URL spellings.
# bash-3.2 compatible: no associative arrays, no ${var,,}.
git_identity_for() {
    local owner="$1" host="$2" ident
    owner=$(printf '%s' "$owner" | tr '[:upper:]' '[:lower:]')
    host=$(printf '%s' "$host" | tr '[:upper:]' '[:lower:]')
    host="${host%:443}"   # the explicit default port git passes for an https URL spelled with it
    ident=$(printf '%s' "$GIT_ORG_IDENTITIES" | awk -F'\t' -v o="$owner" '$1==o{print $2"\t"$3; exit}')
    [ -n "$ident" ] || ident=$(printf '%s' "$GIT_HOST_IDENTITIES" | awk -F'\t' -v h="$host" '$1==h{print $2"\t"$3; exit}')
    printf '%s' "$ident"
}

# stamp_repo_identity <container> <repo_name> <name> <email>
# Stamp the author into one repo as repo-local user.name/user.email, via
# docker exec into the running container (the bootstrap runs host-side). Skips
# repos that are not checked out yet (the clone above failed) and silently
# does nothing when neither field is set. Never fatal: attribution is
# cosmetic, an exec hiccup must not fail the up.
stamp_repo_identity() {
    local container="$1" repo_name="$2" id_name="$3" id_email="$4"
    [ -n "$id_name" ] || [ -n "$id_email" ] || return 0
    docker exec -e "REPO_NAME=$repo_name" -e "ID_NAME=$id_name" -e "ID_EMAIL=$id_email" \
        -u coder "$container" bash -c '
            d="/workspace/repos/$REPO_NAME"; [ -d "$d/.git" ] || exit 0
            [ -n "$ID_NAME" ]  && git -C "$d" config user.name  "$ID_NAME"
            [ -n "$ID_EMAIL" ] && git -C "$d" config user.email "$ID_EMAIL"
            :' || true
}
