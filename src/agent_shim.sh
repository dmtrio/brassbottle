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
write_agent_shim() {
    local dest="$1" binary="$2"
    printf '#!/bin/bash\nAGENT=%s\nKEYS="$HOME/.agent-keys"\nset -a\n[ -f "$KEYS/common.env" ] && . "$KEYS/common.env"\n[ -f "$KEYS/$AGENT.env" ] && . "$KEYS/$AGENT.env"\nset +a\nREAL=$(type -aP %s | grep -v ".agent-shims" | head -1)\n[ -n "$REAL" ] || { echo "%s is not installed in this container" >&2; exit 127; }\nexec "$REAL" "$@"\n' \
        "$binary" "$binary" "$binary" > "$dest"
    chmod +x "$dest"
}
