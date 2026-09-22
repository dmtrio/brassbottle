#!/bin/bash
# src/agent_shim.sh — the identity shim the Dockerfile bakes for every
# mcp-capable enabled agent (and tests/bash.test.sh drives through the same
# function, so the test runs the code production runs — never a copy).
#
# The shim loads per-agent MCP credentials from ~/.agent-keys/<agent>.env,
# OVERRIDING inherited env, then execs the real binary: per-agent identity
# (attribution in tools like Obsidian Annotated) and safe delegation — an
# agent spawning another never passes its own credentials along. <agent>.env
# is COMPLETE (env-scoped + agent-scoped secrets + git routing composed by
# up.sh); common.env was retired in Plugins v2 Phase 3 and the shim still
# sources it when present as a one-release transitional guard.
#
# Sourcing only SETS what the file says — it never clears. Agents are
# launched from interactive shells (VS Code terminal, herdr, tmux), and the
# user's shell exports ~/.agent-keys/user.env (the image's .bashrc piece);
# a shimmed agent spawning another passes its own env along. So before
# sourcing anything the shim CLEARS the git-credential lane — the block is
# INLINED verbatim from src/clear-git-identity.sh (the shim runs before
# anything is sourced, so it cannot source the file; the marked block is
# byte-pinned against that file by tests/bash.test.sh so the two launchers
# cannot drift) — leaving this process carrying exactly what its own file
# says and nothing inherited from a parent's identity (git-credential-org
# reads the table from the process env; gh reads the token variables).
write_agent_shim() {
    local dest="$1" binary="$2"
    printf '#!/bin/bash\nAGENT=%s\nKEYS="$HOME/.agent-keys"\n# Clear the inherited git-credential lane BEFORE sourcing anything: the\n# launching shell (or parent shim) may carry another identity'"'"'s routing, and\n# this process must carry exactly what its own file says. The block below is\n# byte-identical with src/clear-git-identity.sh (pinned by tests).\n# djinn: clear inherited git identity (BEGIN) — byte-identical in the shim template\n_clear_saved_flags=$-\n_clear_prev_table=$GIT_HOST_TOKENS\nunset -v GH_TOKEN GITHUB_TOKEN GH_ENTERPRISE_TOKEN GITHUB_ENTERPRISE_TOKEN GIT_HOST_TOKENS\nset -f\nfor _clear_pair in ${_clear_prev_table:-}; do\n    case $_clear_pair in *=*) unset -v "${_clear_pair#*=}" ;; esac\ndone\ncase $_clear_saved_flags in *f*) ;; *) set +f ;; esac\nunset -v _clear_pair _clear_prev_table _clear_saved_flags\n# djinn: clear inherited git identity (END)\nset -a\n[ -f "$KEYS/common.env" ] && . "$KEYS/common.env"\n[ -f "$KEYS/$AGENT.env" ] && . "$KEYS/$AGENT.env"\nset +a\nREAL=$(type -aP %s | grep -v ".agent-shims" | head -1)\n[ -n "$REAL" ] || { echo "%s is not installed in this container" >&2; exit 127; }\nexec "$REAL" "$@"\n' \
        "$binary" "$binary" "$binary" > "$dest"
    chmod +x "$dest"
}
