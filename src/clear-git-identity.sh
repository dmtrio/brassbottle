# src/clear-git-identity.sh — clear the inherited git-credential lane.
#
# Sourced by src/user-keys-landing.bashrc (the user's interactive shell,
# via the image's .bashrc) and INLINED verbatim into the generated agent
# shims (src/agent_shim.sh, which runs before anything is sourced and
# therefore cannot source this file) — the marked block below is byte-pinned
# against the shim by tests/bash.test.sh so the two launchers cannot drift.
#
# A process descending from another identity's environment would otherwise
# carry that parent's credential lane: an interactive shell inside a tmux
# server an agent started keeps the agent's GH_TOKEN (gh acts as the agent),
# and an agent spawning another passes its own routing along. This block
# unsets the four gh-honoured token variables, GIT_HOST_TOKENS, and every
# variable the INHERITED table names (the host=VAR pairs of the value
# present at that moment) — then the caller's own sourced file sets exactly
# what it owns. git-credential-org reads the table from the process env;
# gh reads the token variables from it.

# djinn: clear inherited git identity (BEGIN) — byte-identical in the shim template
_clear_saved_flags=$-
_clear_prev_table=$GIT_HOST_TOKENS
unset -v GH_TOKEN GITHUB_TOKEN GH_ENTERPRISE_TOKEN GITHUB_ENTERPRISE_TOKEN GIT_HOST_TOKENS
set -f
for _clear_pair in ${_clear_prev_table:-}; do
    case $_clear_pair in *=*) unset -v "${_clear_pair#*=}" ;; esac
done
case $_clear_saved_flags in *f*) ;; *) set +f ;; esac
unset -v _clear_pair _clear_prev_table _clear_saved_flags
# djinn: clear inherited git identity (END)
