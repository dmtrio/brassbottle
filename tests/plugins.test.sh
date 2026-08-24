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
emit_agents_section() {
    echo "---agents---"
    for af in agents/*/agent.yml; do
        [ -e "$af" ] || continue
        printf '%s\t' "$(basename "$(dirname "$af")")"
        yq -o=json -I=0 "$af"
    done
}

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
            emit_agents_section
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
[ "$(yq '.plugins | tag' bottles/TEMPLATE.yml)" = "!!seq" ] \
    && pass "TEMPLATE.yml has a plugins: list" \
    || fail "TEMPLATE.yml is missing the plugins: [] key"
# TEMPLATE must pass the real validator end-to-end
{
    yq -o=json -I=0 bottles/TEMPLATE.yml
    for f in plugins/*/plugin.yml; do
        [ -e "$f" ] || continue
        printf '%s\t' "$(basename "$(dirname "$f")")"
        yq -o=json -I=0 "$f"
    done
    emit_agents_section
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
        emit_agents_section
    } | python3 src/manifest.py --derive
) \
    && pass "all shipped plugins coexist (no cross-plugin dup/reserved names)" \
    || fail "shipped plugins conflict as a set"
# Shipped egress must reach the derived EGRESS (a renamed/mis-indented
# egress: key would otherwise pass every check and firewall the plugin).
eval "$ALL_DERIVED"
EGRESS_ALL="$EGRESS"
echo ",$EGRESS_ALL," | grep -qF ",blob.core.windows.net," \
    && pass "serena's egress folds into derived EGRESS" \
    || fail "serena egress missing from EGRESS: '$EGRESS_ALL'"
# The eval interface is name-based and evals to empty on a rename — pin the
# full emitted variable set. grep first: quoted multi-line values (e.g.
# PLUGIN_MCP_ENTRIES) have continuation lines that are not assignments.
EMITTED=$(printf '%s\n' "$ALL_DERIVED" | grep -oE '^[A-Z_]+=' | tr -d = | LC_ALL=C sort | tr '\n' ' ')
EXPECTED="AGENTS_COMPOSE_YAML AGENTS_ENABLED AGENTS_MCP_JSON AGENT_SECRETS AGENT_SERVERS_JSON AGENT_SERVER_REMOTE_SLOTS AGENT_SERVER_SLOTS CONTAINER_NTFY_TOPIC CONTAINER_NTFY_URL EGRESS EGRESS_CIDRS FORGE GIT_ORG_IDENTITIES GIT_ORG_TOKENS GIT_TOKEN_SOURCE GIT_USER_EMAIL GIT_USER_NAME HOST_MCP_PORTS LITERAL_KEY_AGENTS MEM_LIMIT MOSH_PORTS MOSH_PORTS_DASH PLUGINS PLUGIN_COMPOSE_YAML PLUGIN_ENV_SECRETS PLUGIN_MCP_ENTRIES REMOTE_MOSH REMOTE_NOTIFY REMOTE_TMUX REPOS SHIM_AGENTS SSH_BIND SSH_PORT "
[ "$EMITTED" = "$EXPECTED" ] \
    && pass "--derive emits exactly the variable set up.sh consumes" \
    || fail "emitted variable set changed (update up.sh consumers + this pin): $EMITTED"

echo "── static compose volumes + descriptor-derived agent volumes"
# Static compose stays lean (workspace + gh-auth). manifest.py reserves this
# STATIC set; enabled agents' state_dirs arrive via AGENTS_COMPOSE_YAML.
COMPOSE_STATIC_VOLS=$(yq -r '.volumes | keys | .[]' compose/docker-compose.local.yml | LC_ALL=C sort | tr '\n' ' ')
MANIFEST_STATIC_VOLS=$(python3 -c 'import sys; sys.path.insert(0, "src"); import manifest; print(" ".join(sorted(manifest.STATIC_COMPOSE_VOLUME_NAMES)) + " ")')
[ "$COMPOSE_STATIC_VOLS" = "$MANIFEST_STATIC_VOLS" ] \
    && pass "manifest.py STATIC_COMPOSE_VOLUME_NAMES matches static compose volumes" \
    || fail "compose static volumes '$COMPOSE_STATIC_VOLS' != manifest.py STATIC '$MANIFEST_STATIC_VOLS'"

COMPOSE_STATIC_TARGETS=$(yq -r '.services.djinn.volumes[]' compose/docker-compose.local.yml \
    | awk -F: '{print $2}' | LC_ALL=C sort -u | tr '\n' ' ')
MANIFEST_STATIC_TARGETS=$(python3 -c 'import sys; sys.path.insert(0, "src"); import manifest; print(" ".join(sorted(manifest.STATIC_COMPOSE_MOUNT_PATHS)) + " ")')
[ "$COMPOSE_STATIC_TARGETS" = "$MANIFEST_STATIC_TARGETS" ] \
    && pass "manifest.py STATIC_COMPOSE_MOUNT_PATHS matches static compose targets" \
    || fail "compose static targets '$COMPOSE_STATIC_TARGETS' != manifest.py STATIC '$MANIFEST_STATIC_TARGETS'"

AGENT_DECLARED_VOLS=$(for af in agents/*/agent.yml; do yq -r '.state_dirs[]?.volume // ""' "$af"; done \
    | awk 'NF' | LC_ALL=C sort -u | tr '\n' ' ')
AGENT_DECLARED_TARGETS=$(for af in agents/*/agent.yml; do yq -r '.state_dirs[]?.path // ""' "$af"; done | awk 'NF' \
    | sed 's#^#/home/coder/#' | LC_ALL=C sort -u | tr '\n' ' ')
AGENTS_OVERLAY_VOLS=$(printf '%s\n' "$AGENTS_COMPOSE_YAML" | yq -r '.volumes | keys | .[]' \
    | LC_ALL=C sort -u | tr '\n' ' ')
AGENTS_OVERLAY_TARGETS=$(printf '%s\n' "$AGENTS_COMPOSE_YAML" | yq -r '.services.djinn.volumes[]' \
    | awk -F: '{print $2}' | LC_ALL=C sort -u | tr '\n' ' ')
[ "$AGENT_DECLARED_VOLS" = "$AGENTS_OVERLAY_VOLS" ] \
    && pass "AGENTS_COMPOSE_YAML volume names match agents/*/agent.yml state_dirs" \
    || fail "agent state volume drift: declared '$AGENT_DECLARED_VOLS' overlay '$AGENTS_OVERLAY_VOLS'"
[ "$AGENT_DECLARED_TARGETS" = "$AGENTS_OVERLAY_TARGETS" ] \
    && pass "AGENTS_COMPOSE_YAML mount targets match agents/*/agent.yml state_dirs" \
    || fail "agent state target drift: declared '$AGENT_DECLARED_TARGETS' overlay '$AGENTS_OVERLAY_TARGETS'"
# The generated overlay must be real compose input, not just well-formed YAML:
# render a shipped plugin's declaration and let compose itself validate it.
VOL_DERIVED=$(
    {
        printf '{"plugins": ["codebase-memory"]}\n'
        printf 'codebase-memory\t'; yq -o=json -I=0 plugins/codebase-memory/plugin.yml
        emit_agents_section
    } | python3 src/manifest.py --derive
) || fail "--derive exited non-zero on a volume-declaring plugin"
eval "$VOL_DERIVED"
OVERLAY=$(mktemp -t plugins-overlay.XXXXXX)
printf '%s\n' "$PLUGIN_COMPOSE_YAML" > "$OVERLAY"
# has() on the PARENT map, not `.volumes["x"] | has(...)`: a missing key and a
# key with an empty value are both null, so testing the child's contents passes
# on any file at all — including one with no volumes: section.
yq -e '.volumes | has("cbm-cache")' "$OVERLAY" >/dev/null 2>&1 \
    && pass "overlay declares the plugin's named volume" \
    || fail "overlay missing the cbm-cache volume: $(cat "$OVERLAY")"
if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    # config merges the overlay onto the real compose file exactly as up.sh
    # does, so a shape compose rejects fails HERE, not on someone's next up.
    MERGED=$(CONTAINER_NAME=t USER_UID=1000 USER_GID=1000 RULES_PATH=/tmp KEYS_PATH=/tmp \
        ARTIFACTS_PATH=/tmp BROWSER_TMP_PATH=/tmp IMAGE_TAG=t MEM_LIMIT=2g \
        docker compose -p plugins-test --project-directory "$SCRIPT_DIR" \
            -f compose/docker-compose.local.yml -f "$OVERLAY" config --format json 2>&1) \
        && pass "compose accepts the generated overlay (merged with the real file)" \
        || fail "compose rejected the generated overlay: $(printf '%s' "$MERGED" | head -3)"
    # Assert the RESOLVED mount, not just that the string appears: compose
    # parses a 1-character source as a Windows drive letter, producing a mount
    # with the whole spec as `target` and NO `source`. That config exits 0 and
    # still contains the name — only `docker up` fails, on someone else's
    # machine. Pin source AND target so that shape can never pass again.
    printf '%s' "$MERGED" | jq -e '
        .services["djinn"].volumes
        | map(select(.source == "cbm-cache"))
        | length == 1
        and .[0].target == "/home/coder/.cache/codebase-memory-mcp"
        and .[0].type == "volume"' >/dev/null 2>&1 \
        && pass "merged mount resolves to source=cbm-cache with the declared target" \
        || fail "mount did not resolve: $(printf '%s' "$MERGED" | jq -c '.services["djinn"].volumes' 2>/dev/null)"
    printf '%s' "$MERGED" | jq -e '
        .services["djinn"].environment.PLUGIN_VOLUME_PATHS
        == "/home/coder/.cache/codebase-memory-mcp"' >/dev/null 2>&1 \
        && pass "merged config keeps PLUGIN_VOLUME_PATHS for the entrypoint" \
        || fail "PLUGIN_VOLUME_PATHS lost/mangled when compose merged the overlay"
else
    echo "  ~ SKIP: docker compose unavailable (overlay merge unverified)"
fi
rm -f "$OVERLAY"
# The chown loop is the other half of the contract: without it a fresh volume
# mounts root-owned and the coder-run agent silently cannot write to it.
grep -q 'PLUGIN_VOLUME_PATHS' src/entrypoint.sh \
    && pass "entrypoint consumes PLUGIN_VOLUME_PATHS" \
    || fail "src/entrypoint.sh no longer reads PLUGIN_VOLUME_PATHS (volumes mount root-owned)"
# ...and run the REAL block (extracted from entrypoint.sh, not a copy of it) as
# root in a container. A grep alone cannot see word splitting, globbing, or the
# parent walk — the three things that decide whether the mountpoint the agent
# actually writes to ends up coder-owned.
if command -v docker >/dev/null 2>&1; then
    ENTRY_BLOCK=$(mktemp -t entry-block.XXXXXX)
    awk '/^# ── Plugin-declared volume mountpoints/,/^set \+f$/' src/entrypoint.sh > "$ENTRY_BLOCK"
    [ -s "$ENTRY_BLOCK" ] && grep -q 'set +f' "$ENTRY_BLOCK" \
        || fail "could not extract the volume block from src/entrypoint.sh (markers moved)"
    VOL_OUT=$(docker run --rm -v "$ENTRY_BLOCK:/block.sh:ro" ubuntu:24.04 bash -c '
        set -e
        # ubuntu:24.04 ships its own uid-1000 user; the image builds coder there.
        userdel -f ubuntu 2>/dev/null || true
        useradd -m -u 1000 coder
        # A real glob target the loop must NOT touch, and a deep path whose
        # parents docker would have created root-owned.
        mkdir -p /home/coder/decoy && chown root:root /home/coder/decoy
        mkdir -p /home/coder/.local && chown coder:coder /home/coder/.local
        mkdir -p "/home/coder/.local/state/deep/db" /home/coder/plain
        chown -R root:root /home/coder/.local/state /home/coder/plain
        export PLUGIN_VOLUME_PATHS="/home/coder/plain /home/coder/.local/state/deep/db /home/coder/*"
        . /block.sh
        echo "mountpoint=$(stat -c %U /home/coder/plain)"
        echo "deep=$(stat -c %U /home/coder/.local/state/deep/db)"
        echo "deep_parent=$(stat -c %U /home/coder/.local/state/deep)"
        echo "walk_stopped=$(stat -c %U /home/coder/.local)"
        echo "glob_literal=$([ -d "/home/coder/*" ] && echo yes || echo no)"
        echo "decoy=$(stat -c %U /home/coder/decoy)"
    ' 2>&1) || fail "entrypoint volume block failed to run: $(printf '%s' "$VOL_OUT" | tail -3)"
    printf '%s' "$VOL_OUT" | grep -q 'mountpoint=coder' \
        && pass "entrypoint chowns each declared mountpoint to coder" \
        || fail "mountpoint not chowned: $VOL_OUT"
    printf '%s' "$VOL_OUT" | grep -q 'deep=coder' && printf '%s' "$VOL_OUT" | grep -q 'deep_parent=coder' \
        && pass "entrypoint chowns the root-owned parents docker created" \
        || fail "parent dirs left root-owned: $VOL_OUT"
    printf '%s' "$VOL_OUT" | grep -q 'walk_stopped=coder' \
        && pass "parent walk stops at the first coder-owned ancestor" \
        || fail "parent walk climbed too far: $VOL_OUT"
    # The loop must word-split but NOT glob: with globbing on, '/home/coder/*'
    # expands and chowns unrelated directories while the mounted path is missed.
    printf '%s' "$VOL_OUT" | grep -q 'glob_literal=yes' \
        && printf '%s' "$VOL_OUT" | grep -q 'decoy=root' \
        && pass "entrypoint does not glob-expand a path (set -f holds)" \
        || fail "glob expanded — unrelated dirs chowned, real mountpoint missed: $VOL_OUT"
    rm -f "$ENTRY_BLOCK"
else
    echo "  ~ SKIP: docker unavailable (entrypoint chown loop unverified)"
fi
grep -q 'PLUGIN_COMPOSE_YAML' up.sh \
    && pass "up.sh places the generated overlay" \
    || fail "up.sh no longer consumes PLUGIN_COMPOSE_YAML (declared volumes never mount)"

echo "── derive → build-payload chain (both host halves, real serena + gateway files)"
# A local plugin (serena) + a required remote plugin (gateway): manifest.py
# derives its host_port, resolves the explicit common default for every agent,
# and routes gateway through the per-agent server path.
DERIVED=$(
    {
        printf '{"plugins": ["serena", "gateway"], "common_secrets": ["MCP_GATEWAY_TOKEN"]}\n'
        printf 'serena\t'; yq -o=json -I=0 plugins/serena/plugin.yml
        printf 'gateway\t'; yq -o=json -I=0 plugins/gateway/plugin.yml
        emit_agents_section
    } | PRESENT_SECRET_VARS="MCP_GATEWAY_TOKEN" python3 src/manifest.py --derive
) || fail "--derive exited non-zero on a serena+gateway manifest"
eval "$DERIVED"
[ "$HOST_MCP_PORTS" = "8811" ] \
    && pass "gateway host_port folds into HOST_MCP_PORTS" \
    || fail "HOST_MCP_PORTS wrong: '$HOST_MCP_PORTS'"
printf '%s' "$AGENT_SECRETS" \
    | grep -qF "$(printf 'cursor-agent\tMCP_GATEWAY_TOKEN\tMCP_GATEWAY_TOKEN')" \
    && pass "gateway common default resolves into agent credentials" \
    || fail "AGENT_SECRETS missing gateway default: '$AGENT_SECRETS'"
# Literal-key agents (a literal dialect with env_refs false) are the ones whose
# keys travel as IDENTITY_KEY_n on the exec env. Derived from the descriptors so
# adding or retiring an agent needs no edit here.
LITERAL_IDENTITY_SECRETS=$(i=0; for af in agents/*/agent.yml; do
    yq -e '.mcp | select(.env_refs == false and (.dialect | test("^(url|httpUrl|type-http|serverUrl)$")))' "$af" >/dev/null 2>&1 || continue
    printf '%s:IDENTITY_KEY_%d:MCP_GATEWAY_TOKEN ' "$(yq -r .binary "$af")" "$i"; i=$((i + 1))
done)
PAYLOAD=$(AGENTS_MCP_JSON="$AGENTS_MCP_JSON" PLUGIN_MCP_ENTRIES="$PLUGIN_MCP_ENTRIES" \
    AGENT_SERVERS_JSON="$AGENT_SERVERS_JSON" AGENT_SECRETS="$AGENT_SECRETS" \
    IDENTITY_SECRETS="$LITERAL_IDENTITY_SECRETS" \
    python3 src/wire_plugins.py --build-payload) \
    || fail "--build-payload exited non-zero"
# AGENTS_MCP_JSON is manifest.py's own mcp-capable projection, so this checks
# build_payload against derive output (not a re-implementation here).
EXPECTED_MCP_BINARIES=$(printf '%s' "$AGENTS_MCP_JSON" | jq -c '[.[].binary]')
printf '%s' "$PAYLOAD" | jq -e --argjson exp "$EXPECTED_MCP_BINARIES" '
    ([.agents[] | .binary] == $exp)
    and ([.plugin_mcp_entries[] | keys[0]] == ["serena"])
    and ([.agent_servers[] | .name] == ["coding"])' >/dev/null \
    && pass "derive → build-payload yields descriptor-driven wiring payload" \
    || fail "payload chain output wrong: $PAYLOAD"

echo "── hybrid override chain: obsidian bound to claude + cursor-agent"
A_DERIVED=$(
    {
        printf '{"plugins": ["obsidian-annotated"], "agent_secrets": [{"agent":"claude","slot":"OBSIDIAN_ANNOTATED_KEY","secret":"OBSIDIAN_KEY_a_claude"},{"agent":"cursor-agent","slot":"OBSIDIAN_ANNOTATED_KEY","secret":"OBSIDIAN_KEY_b_cursor_agent"}]}\n'
        printf 'obsidian-annotated\t'; yq -o=json -I=0 plugins/obsidian-annotated/plugin.yml
        emit_agents_section
    } | PRESENT_SECRET_VARS="OBSIDIAN_KEY_a_claude OBSIDIAN_KEY_b_cursor_agent" SECRETS_FILE=/sec/secrets.env python3 src/manifest.py --derive
) || fail "--derive exited non-zero on an agent_secrets manifest"
eval "$A_DERIVED"
[ "$AGENT_SERVER_SLOTS" = "OBSIDIAN_ANNOTATED_KEY" ] \
    && pass "obsidian-annotated derives a required server slot" \
    || fail "AGENT_SERVER_SLOTS wrong: '$AGENT_SERVER_SLOTS'"
# up.sh's wiring loop: only descriptor-marked literal-key agents (the derived
# LITERAL_KEY_AGENTS) get a literal-key env mapping; ref-style agents (claude,
# kimi) retain the ${SLOT} reference from their shim env.
A_IDA=""; A_IDENV=(); i=0
while IFS=$'\t' read -r agent slot source; do
    [ -n "$agent" ] || continue
    case " $AGENT_SERVER_SLOTS " in *" $slot "*) ;; *) continue ;; esac
    case " $LITERAL_KEY_AGENTS " in
        *" $agent "*) A_IDENV+=(-e "IDENTITY_KEY_${i}=v$i"); A_IDA="${A_IDA:+$A_IDA }$agent:IDENTITY_KEY_$i:$slot"; i=$((i+1)) ;;
        *) ;;
    esac
done <<AEOF
$AGENT_SECRETS
AEOF
A_PAYLOAD=$(AGENTS_MCP_JSON="$AGENTS_MCP_JSON" AGENT_SERVERS_JSON="$AGENT_SERVERS_JSON" AGENT_SECRETS="$AGENT_SECRETS" IDENTITY_SECRETS="$A_IDA" python3 src/wire_plugins.py --build-payload) \
    || fail "--build-payload exited non-zero on agent_servers"
printf '%s' "$A_PAYLOAD" | jq -e '
    (.agent_servers | length) == 1
    and .agent_servers[0].name == "obsidian-annotated"
    and .agent_servers[0].ref == ["claude"]
    and (.agent_servers[0].literal == [{agent: "cursor-agent", key_envs: {OBSIDIAN_ANNOTATED_KEY: "IDENTITY_KEY_0"}}])' >/dev/null \
    && pass "hybrid overrides → build-payload yields per-agent obsidian wiring" \
    || fail "agent_servers payload wrong: $A_PAYLOAD"

echo "── python unit tests (src/manifest.py + src/wire_plugins.py)"
UNIT_OUT=$(python3 -m unittest discover -s tests 2>&1) \
    && pass "python3 -m unittest discover -s tests" \
    || { fail "unit tests failed:"; printf '%s\n' "$UNIT_OUT" | tail -30; }

echo "── bundled skill: djinn-bottle checker"
# The skill's checker streams manifest.py --derive the same payload up.sh
# builds; its tests pin that contract, and TEMPLATE.yml must pass the checker
# end-to-end (warnings allowed, errors not).
SKILL_OUT=$(cd rules/skills/djinn-bottle && python3 -m unittest test_check_manifest 2>&1) \
    && pass "rules/skills/djinn-bottle unit tests" \
    || { fail "skill unit tests failed:"; printf '%s\n' "$SKILL_OUT" | tail -20; }
TPL_OUT=$(python3 rules/skills/djinn-bottle/check_manifest.py bottles/TEMPLATE.yml --brassbottle . 2>&1) \
    && pass "check_manifest.py accepts bottles/TEMPLATE.yml" \
    || { fail "TEMPLATE.yml fails the skill checker:"; printf '%s\n' "$TPL_OUT" | tail -10; }

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
src/pull_manifests.py" "$BOTTLES_PATH"
python3 /usr/local/lib/djinn/wire_plugins.py
PRESENT_SECRET_VARS
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
# Shim agents come from agents/*/agent.yml (enabled + mcp-capable), not a
# hardcoded Dockerfile loop. Pin the descriptor-driven contract end-to-end.
EXPECTED_SHIMS=$(for f in agents/*/agent.yml; do
    [ -e "$f" ] || continue
    yq -e '.mcp' "$f" >/dev/null 2>&1 || continue
    yq -r '.binary' "$f"
done | LC_ALL=C sort | tr '\n' ' ' | sed 's/ $//')
DERIVED_SHIMS_SORTED=$(printf '%s\n' $SHIM_AGENTS | LC_ALL=C sort | tr '\n' ' ' | sed 's/ $//')
[ "$DERIVED_SHIMS_SORTED" = "$EXPECTED_SHIMS" ] \
    && pass "manifest-derived SHIM_AGENTS matches mcp binaries in agents/*/agent.yml" \
    || fail "SHIM_AGENTS ($DERIVED_SHIMS_SORTED) != descriptor mcp binaries ($EXPECTED_SHIMS)"
grep -qF '/opt/agents/*/agent.yml' Dockerfile \
    && grep -qF "yq -e '.mcp'" Dockerfile \
    && grep -qF "binary=\"\$(yq -r '.binary' \"\$f\")\"" Dockerfile \
    && pass "Dockerfile shim loop is descriptor-driven (agents glob + mcp + binary)" \
    || fail "Dockerfile shim loop no longer follows agents/*/agent.yml descriptors"
grep -qF 'write_keyfiles "$KEYS_PATH" "$SHIM_AGENTS"' up.sh \
    && ! grep -qF "$EXPECTED_SHIMS" up.sh \
    && pass "up.sh consumes derived SHIM_AGENTS (no hardcoded shim list)" \
    || fail "up.sh shim wiring drifted (expected derived SHIM_AGENTS)"
# update-agent-keys.sh derives its valid-agent set at runtime from the
# container's keys/<name>/*.env files (behavioral coverage in tests/bash.test.sh,
# seeded from the descriptors) — the only drift to pin here is a REINTRODUCED
# hardcoded list.
awk '/^SHIM_AGENTS=/{found=1} END{exit found}' bin/update-agent-keys.sh \
    && pass "update-agent-keys.sh has no hardcoded SHIM_AGENTS list (derives from keys dir)" \
    || fail "update-agent-keys.sh reintroduced a static SHIM_AGENTS list — derive from keys/<name>/*.env instead"
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
grep -qF -- 'ARG AGENTS_ENABLED=""' Dockerfile \
    && pass "Dockerfile AGENTS_ENABLED default is fail-closed (empty)" \
    || fail "Dockerfile AGENTS_ENABLED default is no longer empty"
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
grep -qF -- "python3 /usr/local/lib/djinn/code_workspace.py /workspace/dev.code-workspace" up.sh \
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
