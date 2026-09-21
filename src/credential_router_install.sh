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
#   <runner>   how to execute one shell string as the git-owning user.
#              DEFAULT (the entrypoint, which passes no runner):
#              _router_run_as_coder, dropping to coder via su so the config
#              lands in coder's HOME. Tests pass a plain runner that
#              executes the string in a sandbox HOME — the default path is
#              itself tested through a su stub, because it is the only path
#              production uses.
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
# bottle whose repos: changed converges too.
#
# Failure is LOUD: the only tolerated failure is the --unset-all of a key
# that does not exist (first boot, a changed repos:); a failed add makes the
# function return non-zero and name the origin, and the entrypoint treats
# that as a fatal boot error — without the router, git would fall back to
# the desktop bridge and authenticate as the human.
#
# Runs in the Linux image (bash ≥ 4); plain shell throughout — no
# associative arrays, no ${var,,} — so host-side debugging stays easy.

# The DEFAULT runner: the entrypoint runs as root, and every git config must
# land in the agent user's global config. A named function at file scope (NOT
# a local function shadowing a variable of the same name — invoking "$runner"
# when a function was defined into it runs the empty string: "command not
# found" on every line, rc 0 at the end, router never installed).
_router_run_as_coder() { su -c "$1" coder; }

install_credential_router() {
    local origins="$1" runner="${2:-_router_run_as_coder}"
    [ -n "$origins" ] || return 0
    (
        set -f   # values are manifest-validated, but word-splitting must not glob
        failed=0
        for origin in $origins; do
            # Tolerated: the key may not exist yet (first boot, a re-created
            # bottle whose repos: changed).
            "$runner" "git config --global --unset-all credential.'$origin'.helper" 2>/dev/null || true
            if ! "$runner" "git config --global --add credential.'$origin'.helper ''" \
               || ! "$runner" "git config --global --add credential.'$origin'.helper /usr/local/bin/git-credential-org"; then
                echo "install_credential_router: could not install the credential router for '$origin' — git would fall back to the desktop credential bridge (the human's login)" >&2
                failed=1
            fi
        done
        exit $failed
    )
}
