#!/bin/bash
# tests/bash.test.sh — unit tests for the host-side bash that holds real logic.
# Hand-rolled execute-and-assert (same style as plugins.test.sh; no bats
# dependency). Covers:
#   - src/keyfiles.sh   key-file composition (sourced by up.sh)
#   - common.sh             BASE_PATH resolution (default / env / ./.env / broken)
#   - allow-egress.sh       arg parsing + strict domain validation
#   - update-agent-keys.sh  per-agent key edits (set / remove / common / list)
#   - plugins/*/run.sh      host launchers' token generate-if-missing + persist
#   - src/entrypoint.sh     github.com credential-helper install (idempotent idiom)
# Out of scope: the rest of the container-internal scripts (init-firewall.sh, the
# bulk of entrypoint.sh, mosh-server-wrapper.sh, tmux-*) — they run in a built
# container, and the pure docker orchestration in up.sh (a test would only assert
# "docker ran"). The credential-helper install IS covered because it is pure git
# config logic exercisable against a temp HOME.
set -u

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
FAILURES=0
pass() { echo "  ✓ $1"; }
fail() { echo "  ✗ $1"; FAILURES=$((FAILURES + 1)); }
assert_eq() { if [ "$2" = "$3" ]; then pass "$1"; else fail "$1"; printf '     expected: [%s]\n     got:      [%s]\n' "$2" "$3"; fi; }
assert_rc() { if [ "$2" = "$3" ]; then pass "$1"; else fail "$1 (expected rc $2, got $3)"; fi; }
assert_contains() { case "$2" in *"$3"*) pass "$1" ;; *) fail "$1"; printf '     missing [%s] in: [%s]\n' "$3" "$2" ;; esac; }
assert_absent() { case "$2" in *"$3"*) fail "$1"; printf '     unexpected [%s] in: [%s]\n' "$3" "$2" ;; *) pass "$1" ;; esac; }
mode_of() { stat -c '%a' "$1" 2>/dev/null || stat -f '%Lp' "$1" 2>/dev/null; }

# pwd -P so WORK matches what common.sh computes for CDD_ROOT: it resolves
# symlinks, and macOS puts mktemp dirs under /var/folders (/var → private/var),
# which would make every path-equality assertion below fail on a Mac.
WORK=$(cd "$(mktemp -d)" && pwd -P); trap 'rm -rf "$WORK"' EXIT

# A sandbox copy of the scripts under test, laid out exactly like the repo
# (bin/ + src/) but with NO ./.env. The scripts that resolve a djinn home
# source src/common.sh, which reads the repo root's ./.env BEFORE honouring
# $DJINN_HOME — so running them from $REPO on a machine that has the
# documented `DJINN_HOME=...` in ./.env would ignore our sandbox and write
# to the user's REAL keys/secrets. Running them from here can't.
SBOX="$WORK/repo"; mkdir -p "$SBOX/bin" "$SBOX/src" "$SBOX/plugins/gateway"
cp "$REPO"/bin/*.sh "$SBOX/bin/"; cp "$REPO"/src/common.sh "$SBOX/src/"
# The launcher test drives gateway THROUGH service.sh (the only supported entry
# point): service.sh sources src/common.sh, resolves BASE_PATH, and hands it to
# plugins/<name>/run.sh in the env. Mirror that layout — service.sh at the root,
# common.sh under src/, the launcher under plugins/gateway/. gateway is the
# representative launcher (see the run.sh note below).
cp "$REPO/service.sh" "$SBOX/service.sh"
cp "$REPO"/plugins/gateway/run.sh "$SBOX/plugins/gateway/"

# ────────────────────────────────────────────────────────────────────────────
echo "── src/keyfiles.sh ──"
# shellcheck disable=SC1091
. "$REPO/src/keyfiles.sh"   # defines warn_missing + write_keyfiles, no side effects
SHIM="claude pi gemini cursor-agent codex"

d="$WORK/ck1"; mkdir -p "$d"; chmod 700 "$d"
MCP_GATEWAY_TOKEN=gwval GH_TOKEN=ghval SRC_C=ckey SRC_P=pkey
PES=$(printf 'MCP_GATEWAY_TOKEN\tMCP_GATEWAY_TOKEN\tgateway (run ./service.sh gateway once)\n')
AS=$(printf 'claude\tOBSIDIAN_ANNOTATED_KEY\tSRC_C\npi\tANNOTATED_WATCH_KEY\tSRC_P\n')
write_keyfiles "$d" "$SHIM" "$PES" "$AS" >/dev/null

assert_eq "claude.env = shared + its agent-scoped key" \
    $'MCP_GATEWAY_TOKEN=gwval\nGH_TOKEN=ghval\nOBSIDIAN_ANNOTATED_KEY=ckey' "$(cat "$d/claude.env")"
assert_eq "gemini.env = shared only (no binding)" \
    $'MCP_GATEWAY_TOKEN=gwval\nGH_TOKEN=ghval' "$(cat "$d/gemini.env")"
assert_eq "pi.env carries its watch key" \
    $'MCP_GATEWAY_TOKEN=gwval\nGH_TOKEN=ghval\nANNOTATED_WATCH_KEY=pkey' "$(cat "$d/pi.env")"
assert_eq "every shim agent gets a file" "5" "$(ls "$d"/*.env | wc -l | tr -d ' ')"
assert_absent "no common.env written" "$(ls "$d")" "common.env"
assert_eq "files are mode 600" "600" "$(mode_of "$d/claude.env")"
unset MCP_GATEWAY_TOKEN GH_TOKEN SRC_C SRC_P

# missing source var → warn, and the slot is NOT written
d="$WORK/ck2"; mkdir -p "$d"; chmod 700 "$d"
PES=$(printf 'MISSING_TOK\tMISSING_TOK\tgateway (run ./service.sh gateway once)\n')
out=$(write_keyfiles "$d" "claude" "$PES" "")
assert_contains "missing source warns" "$out" "MISSING_TOK not in secrets.env — gateway (run ./service.sh gateway once) will not authenticate"
assert_eq "missing source leaves an empty file" "" "$(cat "$d/claude.env")"

# agent-scoped appended AFTER shared → wins on a name collision when sourced
d="$WORK/ck3"; mkdir -p "$d"; chmod 700 "$d"
FOO=shared BAR=agentval
PES=$(printf 'FOO\tFOO\thint\n')
AS=$(printf 'claude\tFOO\tBAR\n')   # rebinds FOO for claude to BAR's value
write_keyfiles "$d" "claude" "$PES" "$AS" >/dev/null
sourced=$(env -i bash -c 'set -a; . "$1"; set +a; echo "$FOO"' _ "$d/claude.env")
assert_eq "agent-scoped overrides shared on source (last wins)" "agentval" "$sourced"
unset FOO BAR

# per-org tokens (GIT_ORG_TOKENS) fan into the shared block as GH_TOKEN_<owner>,
# alongside the default GH_TOKEN, on every shim agent — this is what
# git-credential-org reads to route by owner.
d="$WORK/ck4"; mkdir -p "$d"; chmod 700 "$d"
GH_TOKEN=defval SRC_VENDOR=vtok SRC_ACME=atok
GOT=$(printf 'vendor\tGH_TOKEN_vendor\tSRC_VENDOR\nacme-corp\tGH_TOKEN_acme_corp\tSRC_ACME\n')
write_keyfiles "$d" "$SHIM" "" "" "$GOT" >/dev/null
assert_eq "per-org tokens land next to GH_TOKEN on each agent" \
    $'GH_TOKEN=defval\nGH_TOKEN_vendor=vtok\nGH_TOKEN_acme_corp=atok' "$(cat "$d/gemini.env")"
assert_eq "per-org fan-out reaches every shim agent" \
    $'GH_TOKEN=defval\nGH_TOKEN_vendor=vtok\nGH_TOKEN_acme_corp=atok' "$(cat "$d/cursor-agent.env")"
unset GH_TOKEN SRC_VENDOR SRC_ACME

# no orgs (empty / omitted git_org_tokens) → only GH_TOKEN, no regression
d="$WORK/ck5"; mkdir -p "$d"; chmod 700 "$d"
GH_TOKEN=defval
write_keyfiles "$d" "claude" "" "" "" >/dev/null
assert_eq "no orgs writes only GH_TOKEN (5th arg empty)" "GH_TOKEN=defval" "$(cat "$d/claude.env")"
write_keyfiles "$d" "claude" "" "" >/dev/null   # 5th arg omitted entirely
assert_eq "no orgs writes only GH_TOKEN (5th arg omitted)" "GH_TOKEN=defval" "$(cat "$d/claude.env")"
unset GH_TOKEN

# ────────────────────────────────────────────────────────────────────────────
echo "── src/git-credential-org.sh ──"
# The in-container credential router: `get` on stdin (protocol/host/path),
# first path segment = owner, return GH_TOKEN_<owner> → GH_TOKEN → gh fallback.
# useHttpPath=true is what gives it the path line. Run the real script.
HELPER="$REPO/src/git-credential-org.sh"
cred() { printf 'protocol=https\nhost=github.com\npath=%s\n' "$1" | env "${@:2}" bash "$HELPER" get; }

out=$(cred vendor/lib.git GH_TOKEN_vendor=vtok GH_TOKEN=defval)
assert_contains "known owner → its per-org token" "$out" "password=vtok"
assert_contains "per-org token uses x-access-token username" "$out" "username=x-access-token"
assert_absent "per-org token is not the default" "$out" "password=defval"

out=$(cred other/repo.git GH_TOKEN=defval)   # no GH_TOKEN_other set
assert_contains "unknown owner → default GH_TOKEN" "$out" "password=defval"

# owner sanitization parity with manifest.py:_canonical_token_var (- → _):
# acme-corp reads GH_TOKEN_acme_corp, not GH_TOKEN_acme-corp.
out=$(cred acme-corp/thing.git GH_TOKEN_acme_corp=atok GH_TOKEN=defval)
assert_contains "hyphenated owner sanitized to GH_TOKEN_acme_corp" "$out" "password=atok"

# case-folding parity: a mixed-case URL owner (github is case-insensitive; the
# manifest lowercases) must read the lowercased GH_TOKEN_<owner>, not fall back.
out=$(cred PlanetExpress/ship.git GH_TOKEN_planetexpress=ptok GH_TOKEN=defval)
assert_contains "mixed-case owner folds to GH_TOKEN_planetexpress" "$out" "password=ptok"
assert_absent "mixed-case owner does not fall back to default" "$out" "password=defval"

# neither the per-org nor the default token set → defer to gh (human login).
# Mock gh so the fallback is deterministic and offline.
mkdir -p "$WORK/ghbin"
cat > "$WORK/ghbin/gh" <<'MOCK'
#!/bin/bash
# gh auth git-credential get: echo a fixed human credential.
[ "$1" = auth ] && { echo "username=human"; echo "password=humantok"; exit 0; }
exit 1
MOCK
chmod +x "$WORK/ghbin/gh"
out=$(printf 'protocol=https\nhost=github.com\npath=nobody/x.git\n' | env -i PATH="$WORK/ghbin:$PATH" bash "$HELPER" get)
assert_contains "no token set → falls back to gh credential" "$out" "password=humantok"

# store/erase are no-ops (stateless helper) — no output, clean exit.
out=$(printf 'protocol=https\nhost=github.com\npath=vendor/lib.git\n' | GH_TOKEN_vendor=vtok bash "$HELPER" store); rc=$?
assert_rc "store is a no-op (rc 0)" 0 "$rc"
assert_eq "store produces no output" "" "$out"

# ────────────────────────────────────────────────────────────────────────────
echo "── entrypoint.sh: github.com credential-helper install ──"
# entrypoint.sh installs git-credential-org as the github.com helper. VS Code's
# dev-container GitHub feature pre-seeds — and can DUPLICATE, across windows/
# re-attaches — credential.'https://github.com'.helper (= !gh auth git-credential)
# before the entrypoint runs. A plain `git config` set then aborts ("cannot
# overwrite multiple values"), the router never lands, and the generic desktop
# credential bridge answers first → git ops leak the human login. The fix is the
# reset(empty)+add idiom: idempotent regardless of prior count, AND the empty
# reset clears the inherited generic bridge so the router leads the chain.
EH="$WORK/ep-home"; mkdir -p "$EH"
# The desktop bridge lives in SYSTEM scope (/etc/gitconfig) in a real container;
# model it there via GIT_CONFIG_SYSTEM so the test is hermetic (independent of the
# host's own /etc/gitconfig) and proves the global reset clears a SYSTEM helper.
SYSCFG="$EH/system.gitconfig"
printf '[credential]\n\thelper = !desktop-bridge\n' > "$SYSCFG"
seed_home() {   # ~/.gitconfig as VS Code's GitHub feature leaves it: a DUPLICATED gh helper
  cat > "$EH/.gitconfig" <<'SEED'
[credential "https://github.com"]
	helper = !gh auth git-credential
	helper = !gh auth git-credential
SEED
}
gc() { HOME="$EH" GIT_CONFIG_SYSTEM="$SYSCFG" git config --global "$@"; }

# Baseline: the OLD plain set is what regressed — prove it fails on the seed so
# the fix below is demonstrably necessary, not cosmetic.
seed_home
out=$(gc credential.'https://github.com'.helper /usr/local/bin/git-credential-org 2>&1); rc=$?
assert_rc "plain set aborts on VS Code's duplicated pre-seed (the reported bug)" 5 "$rc"
assert_contains "…with the multiple-values error" "$out" "cannot overwrite multiple values"

# The fix: reset(empty)+add, verbatim from entrypoint.sh (minus `su … coder`).
install_helper() {
  gc --unset-all credential.'https://github.com'.helper 2>/dev/null || true
  gc --add credential.'https://github.com'.helper ''
  gc --add credential.'https://github.com'.helper /usr/local/bin/git-credential-org
}
seed_home; install_helper; rc=$?
assert_rc "reset+add succeeds despite the duplicated pre-seed" 0 "$rc"
install_helper; rc=$?   # container restart / re-attach runs it again
assert_rc "reset+add is idempotent on re-run" 0 "$rc"

# The payoff: the effective helper chain git would use for a github.com URL is
# ONLY the router — the empty reset dropped the inherited desktop bridge, so no
# human-login leak. --get-urlmatch merges generic+host-specific honouring resets.
eff=$(HOME="$EH" GIT_CONFIG_SYSTEM="$SYSCFG" git config --get-urlmatch credential.helper https://github.com/dmtrio/x.git)
assert_eq "router is the sole effective github.com helper (bridge cleared)" \
    "/usr/local/bin/git-credential-org" "$eff"

# Drift pin: entrypoint.sh must keep the idempotent idiom. If anyone reverts to a
# plain `git config … helper <value>` set, these fail loudly (mirrors the
# up.sh/plugins.test.sh drift pins).
EP=$(cat "$REPO/src/entrypoint.sh")
assert_contains "entrypoint resets the helper list (--unset-all)" \
    "$EP" "--unset-all credential.'https://github.com'.helper"
assert_contains "entrypoint adds an empty reset before the router" \
    "$EP" "--add credential.'https://github.com'.helper ''"
assert_contains "entrypoint adds the router via --add (not a plain set)" \
    "$EP" "--add credential.'https://github.com'.helper /usr/local/bin/git-credential-org"

# ────────────────────────────────────────────────────────────────────────────
echo "── common.sh ──"
# Copy it out so CDD_ROOT is our temp dir (not the repo, whose ./.env we must
# not read) and BASH_SOURCE resolves there. It lives in src/, and CDD_ROOT is
# that dir's PARENT — so mirror the layout: $cfg/src/common.sh → CDD_ROOT=$cfg.
cfg="$WORK/cfg"; mkdir -p "$cfg/src"; cp "$REPO/src/common.sh" "$cfg/src/common.sh"
bp() { env -i DJINN_HOME="${1-}" bash -c '. "$1"; echo "$BASE_PATH"' _ "$cfg/src/common.sh"; }
assert_eq "default BASE_PATH is ./.djinn" "$cfg/.djinn" "$(bp '')"
assert_eq "DJINN_HOME overrides BASE_PATH" "/custom/home" "$(bp /custom/home)"
printf 'DJINN_HOME=%s/from-dotenv\n' "$WORK" > "$cfg/.env"
assert_eq "./.env sets DJINN_HOME" "$WORK/from-dotenv" "$(env -i bash -c '. "$1"; echo "$BASE_PATH"' _ "$cfg/src/common.sh")"
rm -f "$cfg/.env"

# Compat: DEV_AGENT_HOME still overrides BASE_PATH when DJINN_HOME is unset,
# but DJINN_HOME wins when both are set.
bp_dev() { env -i DEV_AGENT_HOME="${1-}" bash -c '. "$1"; echo "$BASE_PATH"' _ "$cfg/src/common.sh"; }
assert_eq "DEV_AGENT_HOME still overrides BASE_PATH (compat)" "/legacy/home" "$(bp_dev /legacy/home)"
assert_eq "DJINN_HOME wins over DEV_AGENT_HOME when both set" "/new/home" \
    "$(env -i DJINN_HOME=/new/home DEV_AGENT_HOME=/legacy/home bash -c '. "$1"; echo "$BASE_PATH"' _ "$cfg/src/common.sh")"

# Auto-detect: default chosen (no override), no ./.djinn yet, but a
# pre-rebrand ./.dev-agent exists → keep using it (and say so on stderr)
# rather than silently starting a second, empty home. A separate sandbox so
# it doesn't interact with $cfg's own (djinn-only) default-path assertions.
adcfg="$WORK/adcfg"; mkdir -p "$adcfg/src" "$adcfg/.dev-agent"
cp "$REPO/src/common.sh" "$adcfg/src/common.sh"
out=$(env -i bash -c '. "$1"; echo "$BASE_PATH"' _ "$adcfg/src/common.sh" 2>"$WORK/ad.err")
assert_eq "pre-existing .dev-agent is auto-detected" "$adcfg/.dev-agent" "$out"
assert_contains "auto-detect note suggests the move" "$(cat "$WORK/ad.err")" "mv .dev-agent .djinn"
# ...but once ./.djinn also exists, the new default wins outright (no note).
mkdir -p "$adcfg/.djinn"
out=$(env -i bash -c '. "$1"; echo "$BASE_PATH"' _ "$adcfg/src/common.sh" 2>"$WORK/ad2.err")
assert_eq "an existing .djinn wins over .dev-agent auto-detect" "$adcfg/.djinn" "$out"
assert_eq "no auto-detect note once .djinn exists" "" "$(cat "$WORK/ad2.err")"

# A failing COMMAND in ./.env (as opposed to an explicit `exit`, which would
# terminate the shell directly) is what common.sh's set +e guard converts into
# a loud exit 1 instead of a silent abort under the caller's set -e.
printf 'false\n' > "$cfg/.env"
out=$(env -i bash -c 'set -e; . "$1"' _ "$cfg/src/common.sh" 2>&1); rc=$?
assert_rc "broken ./.env aborts with exit 1" 1 "$rc"
assert_contains "broken ./.env reports the failure" "$out" "./.env exited non-zero"
rm -f "$cfg/.env"

# CONTAINERS_PATH resolution (mirrors RULES_PATH: override → $BASE_PATH/containers → repo)
cpath() { env -i DJINN_HOME="${1-}" CONTAINERS_PATH="${2-}" bash -c '. "$1"; echo "$CONTAINERS_PATH"' _ "$cfg/src/common.sh"; }
assert_eq "default CONTAINERS_PATH is the repo's containers/" "$cfg/containers" "$(cpath '' '')"
dah="$WORK/dah-cp"; mkdir -p "$dah/containers"
assert_eq "\$BASE_PATH/containers wins when it exists" "$dah/containers" "$(cpath "$dah" '')"
assert_eq "CONTAINERS_PATH env override wins over everything" "/my/private/manifests" "$(cpath "$dah" /my/private/manifests)"
# CONTAINERS_BUNDLED gates whether up.sh may `git pull` the manifests dir. It
# must be 1 for exactly the in-repo fallback: reporting 0 there would have a
# fresh clone try to pull its own checkout (i.e. pull brassbottle).
cbundled() { env -i DJINN_HOME="${1-}" CONTAINERS_PATH="${2-}" bash -c '. "$1"; echo "$CONTAINERS_BUNDLED"' _ "$cfg/src/common.sh"; }
assert_eq "bundled flag set for the repo's own containers/" "1" "$(cbundled '' '')"
assert_eq "bundled flag clear for \$BASE_PATH/containers" "0" "$(cbundled "$dah" '')"
assert_eq "bundled flag clear for a CONTAINERS_PATH override" "0" "$(cbundled "$dah" /my/private/manifests)"

# ────────────────────────────────────────────────────────────────────────────
echo "── allow-egress.sh ──"
run_ae() { ( cd "$SBOX" && env CONTAINERS_PATH="$WORK/no-such-manifests" \
    MOCK_EXISTING_CONTAINERS="${MOCK_EXISTING_CONTAINERS-djinn-mycontainer}" \
    PATH="$WORK/aebin:$PATH" bash bin/allow-egress.sh "$@" ) 2>&1; }
# docker mock: `inspect [-f fmt] <name>` only "succeeds" (State.Running=false,
# so the live-apply path is skipped) for a name in the space-separated
# MOCK_EXISTING_CONTAINERS allowlist (env var the test sets) — everything else
# reports not-found. Tightened from "inspect always succeeds" so the
# djinn-/dev-agent- fallback resolution in allow-egress.sh is actually
# exercised instead of short-circuited by an inspect that always says yes.
mkdir -p "$WORK/aebin"
cat > "$WORK/aebin/docker" <<'MOCK'
#!/bin/bash
case "$1" in
    inspect)
        if [ "$2" = "-f" ]; then name="$4"; else name="$2"; fi
        for c in $MOCK_EXISTING_CONTAINERS; do
            [ "$c" = "$name" ] && { echo false; exit 0; }
        done
        exit 1
        ;;
    ps)      exit 0 ;;
    *)       exit 0 ;;
esac
MOCK
chmod +x "$WORK/aebin/docker"

out=$(run_ae 2>&1); rc=$?
assert_rc "no args → usage rc 1" 1 "$rc"
assert_contains "no args → usage text" "$out" "Usage: ./bin/allow-egress.sh"
out=$(run_ae mycontainer --badflag); rc=$?
assert_rc "unknown flag rc 1" 1 "$rc"
out=$(run_ae mycontainer good.com --save bogus); rc=$?
assert_rc "bad --save value rc 1" 1 "$rc"
assert_contains "bad --save message" "$out" "--save must be yml, firewall, or none"
out=$(run_ae mycontainer 'not_a_domain' --save none); rc=$?
assert_rc "invalid domain rejected rc 1" 1 "$rc"
assert_contains "invalid domain message" "$out" "not valid domain names"
out=$(run_ae mycontainer 'http://x.com' --save none); rc=$?
assert_rc "domain with scheme rejected" 1 "$rc"
out=$(run_ae mycontainer cdn.playwright.dev --save none); rc=$?
assert_rc "valid domain accepted rc 0" 0 "$rc"
assert_contains "valid domain echoed" "$out" "Domains:   cdn.playwright.dev"
assert_contains "short name resolves to the djinn- prefixed container" "$out" "Container: djinn-mycontainer"
out=$(run_ae djinn-mycontainer cdn.playwright.dev --save none)
assert_contains "full djinn- prefixed name also accepted" "$out" "Container: djinn-mycontainer"

# rebrand-transitional: a REAL pre-rebrand container (the djinn- name does
# NOT exist; the dev-agent- name does) resolves via the fallback, using its
# actual (old) name — not a rebuilt-but-nonexistent djinn- name.
MOCK_EXISTING_CONTAINERS="dev-agent-mycontainer"
out=$(run_ae mycontainer cdn.playwright.dev --save none)
assert_contains "pre-rebrand-only container resolves via fallback" "$out" "Container: dev-agent-mycontainer"
assert_contains "fallback use is announced" "$out" "using pre-rebrand container"
out=$(run_ae dev-agent-mycontainer cdn.playwright.dev --save none)
assert_contains "dev-agent- prefixed input also resolves via fallback" "$out" "Container: dev-agent-mycontainer"
unset MOCK_EXISTING_CONTAINERS

# Neither the djinn- nor the dev-agent- name exists → a clear error naming
# both attempts, plus a ps -a hint for both prefixes.
MOCK_EXISTING_CONTAINERS=""
out=$(run_ae ghost cdn.playwright.dev --save none 2>&1); rc=$?
assert_rc "neither name exists → rc 1" 1 "$rc"
assert_contains "error names the djinn- attempt" "$out" "no container named 'djinn-ghost'"
assert_contains "error names the dev-agent- attempt" "$out" "also tried 'dev-agent-ghost'"
assert_contains "not-found hint lists djinn- containers" "$out" "Existing djinn containers:"
assert_contains "not-found hint lists pre-rebrand containers too" "$out" "Existing pre-rebrand containers"
unset MOCK_EXISTING_CONTAINERS

# ────────────────────────────────────────────────────────────────────────────
echo "── update-agent-keys.sh ──"
DAH="$WORK/dah"; KP="$DAH/keys/mysite"; mkdir -p "$KP"
uak() { ( cd "$SBOX" && env DJINN_HOME="$DAH" bash bin/update-agent-keys.sh "$@" ) ; }

uak mysite claude OBSIDIAN_ANNOTATED_KEY sekret >/dev/null
assert_eq "set writes VAR to <agent>.env" "OBSIDIAN_ANNOTATED_KEY=sekret" "$(cat "$KP/claude.env")"
assert_eq "edited file is mode 600" "600" "$(mode_of "$KP/claude.env")"
uak mysite claude OBSIDIAN_ANNOTATED_KEY newval >/dev/null
assert_eq "idempotent replace (one line, new value)" "OBSIDIAN_ANNOTATED_KEY=newval" "$(cat "$KP/claude.env")"
printf '\n' | uak mysite claude OBSIDIAN_ANNOTATED_KEY >/dev/null   # empty value → remove
assert_eq "empty value removes the var" "" "$(cat "$KP/claude.env")"
uak mysite common MCP_GATEWAY_TOKEN shared >/dev/null
allhave=1; for a in claude pi gemini cursor-agent codex; do grep -q '^MCP_GATEWAY_TOKEN=shared$' "$KP/$a.env" || allhave=0; done
assert_eq "common fans out to every shim agent" "1" "$allhave"
out=$(uak mysite 2>&1); rc=$?
assert_rc "list mode rc 0" 0 "$rc"
assert_contains "list mode shows var names" "$out" "MCP_GATEWAY_TOKEN"
out=$(uak nosuchcontainer claude VAR val 2>&1); rc=$?
assert_rc "missing keys dir rc 1" 1 "$rc"
out=$(uak mysite bogusagent VAR val 2>&1); rc=$?
assert_rc "unknown agent rc 1" 1 "$rc"
assert_contains "unknown agent message" "$out" "agent must be one of"

# ────────────────────────────────────────────────────────────────────────────
echo "── run-*.sh token generation ──"
mkdir -p "$WORK/rbin"
cat > "$WORK/rbin/openssl" <<'MOCK'
#!/bin/bash
[ "$1" = rand ] && { echo "DETERMINISTICTOKEN"; exit 0; }
exec /usr/bin/openssl "$@" 2>/dev/null || exit 0
MOCK
cat > "$WORK/rbin/docker" <<'MOCK'
#!/bin/bash
echo "docker $* | AUTH=${MCP_GATEWAY_AUTH_TOKEN:-} KEY=${PROXYMAN_BRIDGE_KEY:-}" >> "$DOCKER_LOG"
MOCK
chmod +x "$WORK/rbin/openssl" "$WORK/rbin/docker"

RDAH="$WORK/rdah"; mkdir -p "$RDAH"; SEC="$RDAH/secrets.env"
# Drive the launcher through service.sh (the entry point): service.sh resolves
# BASE_PATH from DJINN_HOME via common.sh and exports it for run.sh.
run_svc() { ( cd "$SBOX" && env DJINN_HOME="$RDAH" DOCKER_LOG="$WORK/dockerlog" PATH="$WORK/rbin:$PATH" bash service.sh "$1" ) ; }

: > "$WORK/dockerlog"
run_svc gateway >/dev/null 2>&1 || true
assert_contains "gateway self-generates its token into secrets.env" "$(cat "$SEC")" "MCP_GATEWAY_TOKEN=DETERMINISTICTOKEN"
assert_contains "gateway launches docker with the token in env" "$(cat "$WORK/dockerlog")" "AUTH=DETERMINISTICTOKEN"
assert_contains "gateway runs the coding profile on 8811" "$(cat "$WORK/dockerlog")" "gateway run --profile coding --transport streaming --port 8811"

# service.sh resolves + exports BASE_PATH so run.sh needs no path of its own
assert_contains "launcher requires BASE_PATH from service.sh" \
    "$(cd "$SBOX" && bash plugins/gateway/run.sh 2>&1 || true)" \
    "run this launcher via ./service.sh gateway"

# idempotent: a preset token is not regenerated
printf 'MCP_GATEWAY_TOKEN=PRESET\n' > "$SEC"; : > "$WORK/dockerlog"
run_svc gateway >/dev/null 2>&1 || true
assert_eq "preset token kept (one line, unchanged)" "MCP_GATEWAY_TOKEN=PRESET" "$(cat "$SEC")"
assert_contains "preset token passed to docker" "$(cat "$WORK/dockerlog")" "AUTH=PRESET"
# plugins/proxyman/run.sh and plugins/browser/run.sh share this exact
# generate-if-missing+persist logic, but each gates on a macOS app binary
# (/Applications/…) FIRST, so they can't reach the token step on a Linux host —
# gateway (no such gate) is the representative test for the shared pattern.

# ────────────────────────────────────────────────────────────────────────────
echo "── service.sh (host-service dispatcher) ──"
# A throwaway repo layout: service.sh + src/common.sh (service.sh sources it to
# resolve BASE_PATH just before exec) + a plugin that ships a run.sh (echoes its
# forwarded args) and one that doesn't. No ./.env, so the validation/error paths
# never touch it and the exec path resolves BASE_PATH to $SVC/.djinn.
SVC="$WORK/svc"; mkdir -p "$SVC/src" "$SVC/plugins/withsvc" "$SVC/plugins/nosvc"
cp "$REPO/service.sh" "$SVC/service.sh"; cp "$REPO/src/common.sh" "$SVC/src/"
cat > "$SVC/plugins/withsvc/run.sh" <<'MOCK'
#!/bin/bash
echo "ran withsvc args=[$*] base=${BASE_PATH:+set} containers=${CONTAINERS_PATH:+set}"
MOCK
chmod +x "$SVC/plugins/withsvc/run.sh"
: > "$SVC/plugins/nosvc/plugin.yml"
svc() { ( cd "$SVC" && bash service.sh "$@" ) ; }

out=$(svc 2>&1); rc=$?
assert_rc "no arg exits non-zero" 1 "$rc"
assert_contains "no arg lists services with a run.sh" "$out" "withsvc"
assert_absent "no arg omits plugins without a run.sh" "$out" "nosvc"

out=$(svc nonesuch 2>&1); rc=$?
assert_rc "unknown plugin exits non-zero" 1 "$rc"
assert_contains "unknown plugin names the missing dir" "$out" "no plugin named 'nonesuch'"

out=$(svc ../withsvc 2>&1); rc=$?
assert_rc "path-traversal name rejected before any fs lookup" 1 "$rc"
assert_contains "traversal name reported as invalid" "$out" "invalid plugin name"

out=$(svc nosvc 2>&1); rc=$?
assert_rc "plugin without run.sh exits non-zero" 1 "$rc"
assert_contains "plugin without run.sh explains why" "$out" "has no host service"

out=$(svc withsvc chrome --flag 2>&1); rc=$?
assert_rc "valid service execs run.sh (rc 0)" 0 "$rc"
assert_contains "dispatcher forwards extra args verbatim" "$out" "ran withsvc args=[chrome --flag]"
assert_contains "dispatcher exports BASE_PATH to the launcher" "$out" "base=set"
# A launcher that reads a container manifest (browser) must see the SAME
# CONTAINERS_PATH up.sh does; unexported it would silently ignore ./.env.
assert_contains "dispatcher exports CONTAINERS_PATH to the launcher" "$out" "containers=set"

echo ""
if [ "$FAILURES" -gt 0 ]; then echo "FAILED: $FAILURES bash test(s)"; exit 1; fi
echo "all bash tests passed"
