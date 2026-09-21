#!/bin/bash
# src/credential_router_install.sh — the per-origin git credential-router
# install loop, extracted from src/entrypoint.sh so the REAL loop is
# executable in tests (same precedent as src/keyfiles.sh / src/git_notices.sh:
# entrypoint.sh sources this file; the Dockerfile bakes it into the image at
# /usr/local/lib/djinn/credential_router_install.sh).
#
# install_credential_router <GIT_CREDENTIAL_HOSTS> [runner]
#
# Installs git-credential-org as the credential helper for EVERY origin in
# GIT_CREDENTIAL_HOSTS (manifest.py: every host the git.hosts table names,
# every https:// origin in repos:, and the CLI host's origin always — row or
# no row). No host is hard-coded in the loop.
#
#   <origins>  newline/space-separated scheme://host[:port] origins
#              (manifest.py-validated; word-split on purpose)
#   <runner>   how to execute one shell string as the git-owning user. The
#              entrypoint (root) uses the default, which drops to coder via
#              su so the config lands in coder's HOME; tests pass a plain
#              runner that executes the string in a sandbox HOME.
#
# VS Code's dev-container GitHub feature pre-seeds — and can DUPLICATE,
# across windows/re-attaches — credential.'https://github.com'.helper
# (= !gh auth git-credential) before the entrypoint runs. A plain
# `git config` set then aborts ("cannot overwrite multiple values"), the
# router never lands, and the generic desktop credential bridge
# (credential.helper in /etc/gitconfig) answers first → git ops leak the
# human's login. The reset(empty)+add idiom is idempotent regardless of
# prior count AND the empty reset clears the inherited generic bridge, so
# the router leads the chain. Same idiom for every origin, so a re-created
# bottle whose repos: changed converges too. Bash-3.2 compatible: while
# word-splitting, no associative arrays, no ${var,,}.
install_credential_router() {
    local origins="$1" runner="$2"
    [ -n "$origins" ] || return 0
    if [ -z "$runner" ]; then
        # Default (the entrypoint's) runner: the entrypoint runs as root and
        # every git config must land in the agent user's global config.
        runner() { su -c "$1" coder; }
    fi
    (
        set -f   # values are manifest-validated, but word-splitting must not glob
        for origin in $origins; do
            "$runner" "git config --global --unset-all credential.'$origin'.helper" 2>/dev/null || true
            "$runner" "git config --global --add credential.'$origin'.helper ''"
            "$runner" "git config --global --add credential.'$origin'.helper /usr/local/bin/git-credential-org"
        done
    )
    return 0
}
