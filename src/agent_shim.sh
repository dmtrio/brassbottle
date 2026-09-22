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
# sourcing anything the shim CLEARS the git-credential lane — GH_TOKEN,
# GITHUB_TOKEN, GIT_HOST_TOKENS and every variable named by the INHERITED
# GIT_HOST_TOKENS (the pairs of the value present at that moment) — leaving
# this process carrying exactly what its own file says and nothing
# inherited from a parent's identity (git-credential-org reads the table
# from the process env; gh reads GH_TOKEN from it).
write_agent_shim() {
    local dest="$1" binary="$2"
    printf '#!/bin/bash\nAGENT=%s\nKEYS="$HOME/.agent-keys"\n# Clear the inherited git-credential lane BEFORE sourcing anything: the\n# launching shell (or parent shim) may carry another identity'"'"'s routing, and\n# this process must carry exactly what its own file says.\n_inherited_table=$GIT_HOST_TOKENS\nunset GH_TOKEN GITHUB_TOKEN GIT_HOST_TOKENS\nfor _pair in ${_inherited_table:-}; do\n    case $_pair in *=*) unset "${_pair#*=}" ;; esac\ndone\nunset _pair _inherited_table\nset -a\n[ -f "$KEYS/common.env" ] && . "$KEYS/common.env"\n[ -f "$KEYS/$AGENT.env" ] && . "$KEYS/$AGENT.env"\nset +a\nREAL=$(type -aP %s | grep -v ".agent-shims" | head -1)\n[ -n "$REAL" ] || { echo "%s is not installed in this container" >&2; exit 127; }\nexec "$REAL" "$@"\n' \
        "$binary" "$binary" "$binary" > "$dest"
    chmod +x "$dest"
}
