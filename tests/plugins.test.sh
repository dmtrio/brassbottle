#!/bin/bash
# tests/plugins.test.sh — host-runnable checks for the plugin mechanism.
# The validation and wiring LOGIC is real Python now (src/manifest.py,
# src/wire_plugins.py — unit-tested by tests/test_*.py, run below), so this
# suite is down to what only a shell can check: every SHIPPED plugin file
# passes the real validator, the TEMPLATE manifest derives cleanly, the
# derive → build-payload chain holds together, the Dockerfile bake contract
# stands, and up.sh still calls the modules (pin greps). The docker build/up
# path itself is covered by the manual build-test against a throwaway
# manifest (see PLN/LOG - Baked-in Plugins).

# SC2015 (`A && pass || fail` is not if-else): intentional here — pass() is a
# bare echo and cannot fail, so the || arm only runs when the check fails.
# shellcheck disable=SC2015

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "$SCRIPT_DIR"

command -v yq >/dev/null || { echo "SKIP: yq not installed"; exit 0; }
command -v jq >/dev/null || { echo "SKIP: jq not installed"; exit 0; }

FAILURES=0
fail() { echo "  ✗ $1"; FAILURES=$((FAILURES + 1)); }
pass() { echo "  ✓ $1"; }

echo "── syntax"
bash -n up.sh && pass "bash -n up.sh" || fail "up.sh has syntax errors"

if ! command -v python3 >/dev/null; then
    # python3 is a hard up.sh requirement — a green run must never mean
    # "the validation/wiring logic went untested".
    fail "python3 not installed — manifest/wiring tests did NOT run (up.sh requires python3)"
else

echo "── shipped plugin files (validated through the real src/manifest.py)"
found=0
for f in plugins/*/plugin.yml; do
    [ -e "$f" ] || continue
    found=1
    name=$(basename "$(dirname "$f")")
    # Same conversion up.sh feeds --derive, with a manifest enabling just
    # this plugin: the real validator applies every rule (name charset, mcp
    # schema, reserved names, egress hostnames) — no mirrored copies.
    OUT=$(
        {
            printf '{"plugins": ["%s"]}\n' "$name"
            printf '%s\t' "$name"
            yq -o=json -I=0 "$f"
        } | python3 src/manifest.py --derive 2>&1
    ) \
        && pass "$name: passes manifest.py validation" \
        || fail "$name: rejected by manifest.py: $(printf '%s' "$OUT" | head -3)"

    # install: is required iff the plugin has a LOCAL (command:) server — those
    # bake a binary. Remote (url:) and egress-only plugins carry no install:.
    has_local=$(yq -r '[(.mcp // {})[] | select(has("command"))] | length' "$f")
    install=$(yq -r '.install // ""' "$f")
    if [ "${has_local:-0}" != "0" ] && { [ -z "$install" ] || [ "$install" = "null" ]; }; then
        fail "$name: local (command:) server needs an install: block (manifest.py fails derive)"
    else
        pass "$name: install present iff local server"
    fi
done
[ "$found" = 1 ] || fail "no plugin files found under plugins/"

echo "── template"
[ "$(yq '.plugins | tag' containers/TEMPLATE.yml)" = "!!seq" ] \
    && pass "TEMPLATE.yml has a plugins: list" \
    || fail "TEMPLATE.yml is missing the plugins: [] key"
# TEMPLATE must pass the real validator end-to-end
{
    yq -o=json -I=0 containers/TEMPLATE.yml
    for f in plugins/*/plugin.yml; do
        [ -e "$f" ] || continue
        printf '%s\t' "$(basename "$(dirname "$f")")"
        yq -o=json -I=0 "$f"
    done
} | python3 src/manifest.py --derive >/dev/null \
    && pass "TEMPLATE.yml passes manifest.py --derive" \
    || fail "TEMPLATE.yml rejected by manifest.py"

echo "── all shipped plugins as a set (cross-plugin rules)"
# A manifest enabling EVERY shipped plugin: catches two shipped files
# defining the same MCP server name or squatting a reserved one — rules the
# per-file checks above can't see.
ALL_PLUGINS=$(for f in plugins/*/plugin.yml; do [ -e "$f" ] && printf '"%s",' "$(basename "$(dirname "$f")")"; done)
ALL_DERIVED=$(
    {
        printf '{"plugins": [%s]}\n' "${ALL_PLUGINS%,}"
        for f in plugins/*/plugin.yml; do
            [ -e "$f" ] || continue
            printf '%s\t' "$(basename "$(dirname "$f")")"
            yq -o=json -I=0 "$f"
        done
    } | python3 src/manifest.py --derive
) \
    && pass "all shipped plugins coexist (no cross-plugin dup/reserved names)" \
    || fail "shipped plugins conflict as a set"
# Shipped egress must reach the derived EGRESS (a renamed/mis-indented
# egress: key would otherwise pass every check and firewall the plugin).
EGRESS_ALL=$(eval "$ALL_DERIVED"; printf '%s' "$EGRESS")
echo ",$EGRESS_ALL," | grep -qF ",blob.core.windows.net," \
    && pass "serena's egress folds into derived EGRESS" \
    || fail "serena egress missing from EGRESS: '$EGRESS_ALL'"
# The eval interface is name-based and evals to empty on a rename — pin the
# full emitted variable set. grep first: quoted multi-line values (e.g.
# PLUGIN_MCP_ENTRIES) have continuation lines that are not assignments.
EMITTED=$(printf '%s\n' "$ALL_DERIVED" | grep -oE '^[A-Z_]+=' | tr -d = | LC_ALL=C sort | tr '\n' ' ')
EXPECTED="AGENT_SECRETS AGENT_SERVERS_JSON AGENT_SERVER_SLOTS CONTAINER_NTFY_TOPIC CONTAINER_NTFY_URL EGRESS EGRESS_CIDRS FORGE GIT_ORG_IDENTITIES GIT_ORG_TOKENS GIT_TOKEN_SOURCE GIT_USER_EMAIL GIT_USER_NAME HOST_MCP_PORTS INSTALL_AIDER INSTALL_CLAUDE INSTALL_CODEX INSTALL_CURSOR INSTALL_GEMINI INSTALL_PI MEM_LIMIT MOSH_PORTS MOSH_PORTS_DASH PLUGINS PLUGIN_ENV_SECRETS PLUGIN_MCP_ENTRIES REMOTE_MOSH REMOTE_NOTIFY REMOTE_TMUX REPOS SSH_BIND SSH_PORT "
[ "$EMITTED" = "$EXPECTED" ] \
    && pass "--derive emits exactly the variable set up.sh consumes" \
    || fail "emitted variable set changed (update up.sh consumers + this pin): $EMITTED"

echo "── derive → build-payload chain (both host halves, real serena + gateway files)"
# A local plugin (serena) + a remote plugin (gateway): manifest.py derives its
# host_port into HOST_MCP_PORTS, its secret slot into PLUGIN_ENV_SECRETS, and
# its mcp entry into PLUGIN_MCP_ENTRIES alongside serena's.
DERIVED=$(
    {
        printf '{"plugins": ["serena", "gateway"]}\n'
        printf 'serena\t'; yq -o=json -I=0 plugins/serena/plugin.yml
        printf 'gateway\t'; yq -o=json -I=0 plugins/gateway/plugin.yml
    } | python3 src/manifest.py --derive
) || fail "--derive exited non-zero on a serena+gateway manifest"
eval "$DERIVED"
[ "$HOST_MCP_PORTS" = "8811" ] \
    && pass "gateway host_port folds into HOST_MCP_PORTS" \
    || fail "HOST_MCP_PORTS wrong: '$HOST_MCP_PORTS'"
printf '%s' "$PLUGIN_ENV_SECRETS" \
    | grep -qF "$(printf 'MCP_GATEWAY_TOKEN\tMCP_GATEWAY_TOKEN\tgateway')" \
    && pass "gateway env-scoped secret slot derived into PLUGIN_ENV_SECRETS" \
    || fail "PLUGIN_ENV_SECRETS missing gateway slot: '$PLUGIN_ENV_SECRETS'"
PAYLOAD=$(WIRE_CURSOR=true WIRE_GEMINI=yes WIRE_PI=false WIRE_CODEX=true \
    PLUGIN_MCP_ENTRIES="$PLUGIN_MCP_ENTRIES" \
    AGENT_SERVERS_JSON="$AGENT_SERVERS_JSON" IDENTITY_AGENTS="$IDENTITY_AGENTS" \
    python3 src/wire_plugins.py --build-payload) \
    || fail "--build-payload exited non-zero"
printf '%s' "$PAYLOAD" | jq -e '
    .wire == {cursor: true, gemini: false, pi: false, codex: true}
    and ([.plugin_mcp_entries[] | keys[0]] == ["serena", "coding"])
    and (.agent_servers == [])' >/dev/null \
    && pass "derive → build-payload yields the wiring payload (strict booleans: yes/1 stay off)" \
    || fail "payload chain output wrong: $PAYLOAD"

echo "── agent_secrets chain: obsidian bound to claude + cursor-agent (Phase 2)"
A_DERIVED=$(
    {
        printf '{"plugins": ["obsidian-annotated"], "agent_secrets": [{"agent":"claude","slot":"OBSIDIAN_ANNOTATED_KEY","secret":"OBSIDIAN_KEY_a_claude"},{"agent":"cursor-agent","slot":"OBSIDIAN_ANNOTATED_KEY","secret":"OBSIDIAN_KEY_b_cursor_agent"}]}\n'
        printf 'obsidian-annotated\t'; yq -o=json -I=0 plugins/obsidian-annotated/plugin.yml
    } | SECRET_KEY_VARS="OBSIDIAN_KEY_a_claude OBSIDIAN_KEY_b_cursor_agent" SECRETS_FILE=/sec/secrets.env python3 src/manifest.py --derive
) || fail "--derive exited non-zero on an agent_secrets manifest"
eval "$A_DERIVED"
[ "$AGENT_SERVER_SLOTS" = "OBSIDIAN_ANNOTATED_KEY" ] \
    && pass "obsidian-annotated derives an agent-scoped server slot" \
    || fail "AGENT_SERVER_SLOTS wrong: '$AGENT_SERVER_SLOTS'"
# up.sh's wiring loop: claude → ref (empty key_env); cursor-agent → literal key.
A_IDA=""; A_IDENV=(); i=0
while IFS=$'\t' read -r agent slot source; do
    [ -n "$agent" ] || continue
    case " $AGENT_SERVER_SLOTS " in *" $slot "*) ;; *) continue ;; esac
    case "$agent" in
        claude|codex) A_IDA="${A_IDA:+$A_IDA }$agent::$slot" ;;
        *) A_IDENV+=(-e "IDENTITY_KEY_${i}=v$i"); A_IDA="${A_IDA:+$A_IDA }$agent:IDENTITY_KEY_$i:$slot"; i=$((i+1)) ;;
    esac
done <<AEOF
$AGENT_SECRETS
AEOF
A_PAYLOAD=$(AGENT_SERVERS_JSON="$AGENT_SERVERS_JSON" IDENTITY_AGENTS="$A_IDA" python3 src/wire_plugins.py --build-payload) \
    || fail "--build-payload exited non-zero on agent_servers"
printf '%s' "$A_PAYLOAD" | jq -e '
    (.agent_servers | length) == 1
    and .agent_servers[0].name == "obsidian-annotated"
    and .agent_servers[0].claude == true
    and (.agent_servers[0].literal == [{agent: "cursor-agent", key_env: "IDENTITY_KEY_0"}])' >/dev/null \
    && pass "agent_secrets → build-payload yields per-agent obsidian wiring" \
    || fail "agent_servers payload wrong: $A_PAYLOAD"

echo "── python unit tests (src/manifest.py + src/wire_plugins.py)"
UNIT_OUT=$(python3 -m unittest discover -s tests 2>&1) \
    && pass "python3 -m unittest discover -s tests" \
    || { fail "unit tests failed:"; printf '%s\n' "$UNIT_OUT" | tail -30; }

fi  # command -v python3

echo "── up.sh ↔ module contract pins"
# The modules are unit-tested; these greps only prove up.sh still CALLS them
# (and converts YAML with yq) — the last mirror-drift risk left in bash.
while IFS= read -r expr; do
    [ -n "$expr" ] || continue
    grep -qF -- "$expr" up.sh \
        && pass "up.sh still contains: $expr" \
        || fail "up.sh no longer contains (update this suite!): $expr"
done <<'DRIFT'
yq -o=json -I=0 "$MANIFEST"
src/manifest.py" --derive
--build-payload
"$PYTHON3" "$SCRIPT_DIR/src/wire_plugins.py"
python3 /usr/local/lib/dev-agent/wire_plugins.py
OBSIDIAN_(WATCH_)?KEY_|AXIOM_KEY_|AXIOM_TOKEN$
DRIFT
# The identity-key prefixes and hostname rule each live in two places by
# design (bash glue ↔ module, manifest.py ↔ allow-egress.sh) — cross-pin
# them so tightening one side can't silently strand the other.
grep -qF "OBSIDIAN_KEY" src/manifest.py && grep -qF "OBSIDIAN_WATCH_KEY" src/manifest.py \
    && pass "manifest.py uses the same identity-key prefixes up.sh's compgen scans" \
    || fail "identity-key prefixes drifted between up.sh and manifest.py"
DOMAIN_BODY='([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+'
grep -qF -- "$DOMAIN_BODY" bin/allow-egress.sh && grep -qF -- "$DOMAIN_BODY" src/manifest.py \
    && pass "hostname rule matches between manifest.py and allow-egress.sh" \
    || fail "hostname rule drifted between manifest.py and allow-egress.sh"
# The shim-agent list lives in three places (the Dockerfile bakes the shims;
# up.sh writes one <agent>.env per shim agent; update-agent-keys.sh fans 'common'
# across them). Drift would strand an agent with no env file or no override.
SHIM_LIST="claude pi gemini cursor-agent codex"
grep -qF "for a in $SHIM_LIST; do" Dockerfile \
    && grep -qF "SHIM_AGENTS=\"$SHIM_LIST\"" up.sh \
    && grep -qF "SHIM_AGENTS=\"$SHIM_LIST\"" bin/update-agent-keys.sh \
    && pass "shim-agent list matches across Dockerfile, up.sh, update-agent-keys.sh" \
    || fail "shim-agent list drifted (Dockerfile ↔ up.sh ↔ update-agent-keys.sh)"
# ...and it must set-equal manifest.py's AGENT_NAMES (the agents agent_secrets
# may bind). If they drift, a bound agent could get a file with no shared block
# — or a shim agent could be un-bindable.
if command -v python3 >/dev/null; then
    AGENT_NAMES_SORTED=$(python3 -c 'import sys; sys.path.insert(0,"src"); import manifest; print(" ".join(sorted(manifest.AGENT_NAMES)))')
    SHIM_SORTED=$(printf '%s\n' $SHIM_LIST | LC_ALL=C sort | tr '\n' ' ' | sed 's/ $//')
    [ "$AGENT_NAMES_SORTED" = "$SHIM_SORTED" ] \
        && pass "manifest.py AGENT_NAMES set-equals the shim-agent list" \
        || fail "AGENT_NAMES ($AGENT_NAMES_SORTED) != shim agents ($SHIM_SORTED)"
fi
# common.env is retired: up.sh must no longer WRITE it (the shim keeps a
# transitional [ -f ] guard, so the Dockerfile reference is expected).
grep -qE 'common\.env" *$|>> "\$KEYS_PATH/common.env"|> "\$KEYS_PATH/common.env"' up.sh \
    && fail "up.sh still writes common.env (Phase 3 retired it)" \
    || pass "up.sh no longer writes common.env"

echo "── plugin directory layout (packageable plugins)"
# Each plugin is a directory plugins/<name>/plugin.yml (+ optional host-only
# run.sh); the flat plugins/<name>.yml layout is gone. Pin both so a loader
# that silently reverts to the flat glob is caught.
flat=$(find plugins -maxdepth 1 -name '*.yml' -type f 2>/dev/null)
[ -z "$flat" ] \
    && pass "no flat plugins/*.yml remain (all migrated to directories)" \
    || fail "stray flat plugin file(s): $flat"
missing=""
for d in plugins/*/; do
    [ -e "$d/plugin.yml" ] || missing="$missing $(basename "$d")"
done
[ -z "$missing" ] \
    && pass "every plugin directory has a plugin.yml" \
    || fail "plugin dir(s) missing plugin.yml:$missing"
grep -qF -- 'plugins"/*/plugin.yml' up.sh \
    && pass "up.sh globs the directory layout (plugins/*/plugin.yml)" \
    || fail "up.sh no longer globs plugins/*/plugin.yml"

echo "── dockerfile bake"
for f in plugins/*/plugin.yml; do
    [ -e "$f" ] || continue
    # Only local plugins carry an install: block; skip the empty string a
    # remote/config-only plugin yields (bash -n on "" trivially passes anyway).
    yq -r '.install // ""' "$f" | bash -n \
        && pass "$(basename "$(dirname "$f")"): install block is valid bash (or empty)" \
        || fail "$(basename "$(dirname "$f")"): install block fails bash -n"
done
grep -qF -- '/opt/plugins/*/plugin.yml' Dockerfile \
    && pass "Dockerfile bake loop globs the directory layout" \
    || fail "Dockerfile bake loop no longer globs /opt/plugins/*/plugin.yml"
grep -qxF -- 'plugins/*/run.sh' .dockerignore \
    && pass ".dockerignore keeps host-only run.sh launchers out of the image" \
    || fail ".dockerignore no longer excludes plugins/*/run.sh"
grep -qF -- "yq -e -r '.install'" Dockerfile \
    && pass "Dockerfile bake still gates on .install via yq -e" \
    || fail "Dockerfile bake no longer reads .install"
grep -qF -- "config-only, nothing to bake" Dockerfile \
    && pass "Dockerfile bake skips remote (no-install) plugins instead of failing the build" \
    || fail "Dockerfile bake no longer skips no-install plugins (remote plugins would break the build)"
grep -qF -- "COPY src/wire_plugins.py" Dockerfile \
    && pass "Dockerfile bakes src/wire_plugins.py into the image" \
    || fail "Dockerfile no longer bakes wire_plugins.py (up.sh execs it)"
# code_workspace.py merges the repo list into dev.code-workspace (logic is
# unit-tested by tests/test_code_workspace.py; these pins prove the wiring).
grep -qF -- "COPY src/code_workspace.py" Dockerfile \
    && pass "Dockerfile bakes src/code_workspace.py into the image" \
    || fail "Dockerfile no longer bakes code_workspace.py (up.sh execs it)"
grep -qF -- "python3 /usr/local/lib/dev-agent/code_workspace.py /workspace/dev.code-workspace" up.sh \
    && pass "up.sh invokes code_workspace.py against dev.code-workspace" \
    || fail "up.sh no longer wires to code_workspace.py (update this suite!)"
# up.sh sources the extracted key-composition helper and calls it (the logic is
# unit-tested by tests/bash.test.sh; this pin proves up.sh still wires to it).
grep -qF -- '. "$SCRIPT_DIR/src/keyfiles.sh"' up.sh \
    && grep -qF -- 'write_keyfiles "$KEYS_PATH"' up.sh \
    && pass "up.sh sources + calls src/keyfiles.sh" \
    || fail "up.sh no longer wires to src/keyfiles.sh (update this suite!)"

echo "── host-side bash unit tests (tests/bash.test.sh) ──"
BASH_OUT=$(bash "$SCRIPT_DIR/tests/bash.test.sh" 2>&1) \
    && pass "tests/bash.test.sh" \
    || { fail "bash unit tests failed:"; printf '%s\n' "$BASH_OUT" | tail -30; }

echo ""
if [ "$FAILURES" -gt 0 ]; then
    echo "FAILED: $FAILURES check(s)"
    exit 1
fi
echo "all checks passed"
