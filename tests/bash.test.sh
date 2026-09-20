#!/bin/bash
# tests/bash.test.sh — unit tests for the host-side bash that holds real logic.
# Hand-rolled execute-and-assert (same style as plugins.test.sh; no bats
# dependency). Covers:
#   - src/keyfiles.sh   key-file composition (sourced by up.sh)
#   - src/git_notices.sh    up-time git.orgs/egress notices (sourced by up.sh)
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
cp "$REPO"/bin/*.sh "$SBOX/bin/"; cp "$REPO"/src/common.sh "$REPO/src/keyfiles.sh" "$SBOX/src/"
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
. "$REPO/src/keyfiles.sh"   # defines warn_missing + write_keyfiles + git_host_token_pairs, no side effects

# git_host_token_pairs <git_host_tokens>: one VAR=VALUE line per credential the
# bootstrap clone exec needs — GIT_HOST_TOKENS itself plus every variable it
# names, read from the environment by indirect expansion. This is the exact
# mechanism up.sh hands the clone's `docker exec` its env, so the in-container
# git-credential-org sees the same table and token values it would see inside
# the running container. (Bootstrap-clone environment test: this function is
# what the clone exec's env is built from — see the up.sh drift pins below.)
CLONE_DIR="$WORK/clone"; mkdir -p "$CLONE_DIR"
GH_TOKEN=cli_tok SRC_FRY=frytok SRC_X=xtok
out=$(git_host_token_pairs "github.com=GH_TOKEN git.example.test=SRC_FRY h2.test=SRC_X")
assert_eq "clone env carries GIT_HOST_TOKENS and each named variable" \
    $'GIT_HOST_TOKENS=github.com=GH_TOKEN git.example.test=SRC_FRY h2.test=SRC_X\nGH_TOKEN=cli_tok\nSRC_FRY=frytok\nSRC_X=xtok' \
    "$out"
out=$(git_host_token_pairs "github.com=GH_TOKEN git.example.test=SRC_FRY h2.test=SRC_FRY")
assert_eq "clone env dedupes one variable serving two hosts" \
    $'GIT_HOST_TOKENS=github.com=GH_TOKEN git.example.test=SRC_FRY h2.test=SRC_FRY\nGH_TOKEN=cli_tok\nSRC_FRY=frytok' \
    "$out"
# NEW FORM at the clone boundary: a manifest declaring
# git.hosts.github.com.token: GH_TOKEN_x resolves GIT_TOKEN_SOURCE=GH_TOKEN_x
# (up.sh then exports GH_TOKEN from it) and the clone env carries the table
# row variable with that secret's VALUE — no bare GH_TOKEN forward needed.
GH_TOKEN_x=new-token-value
out=$(git_host_token_pairs "github.com=GH_TOKEN_x")
assert_eq "clone env carries the git.hosts.github.com token variable" \
    $'GIT_HOST_TOKENS=github.com=GH_TOKEN_x\nGH_TOKEN_x=new-token-value' \
    "$out"
unset GH_TOKEN_x
unset GH_TOKEN SRC_FRY SRC_X

# warn_unbound_org_token and note_orphan_org_binding are gone with per-owner
# routing (GH_HOST_<owner> bindings no longer exist): a host token is routed
# by the GIT_HOST_TOKENS row up.sh writes from the manifest, so update-agent-
# keys.sh has nothing to warn about — it edits values, never the table.
if grep -q 'warn_unbound_org_token\|note_orphan_org_binding' "$REPO/src/keyfiles.sh" \
    "$REPO/bin/update-agent-keys.sh"; then
    fail "per-org binding warn/note helpers are gone from keyfiles + update-agent-keys"
else
    pass "per-org binding warn/note helpers are gone from keyfiles + update-agent-keys"
fi

# The shim-agent list derives from the descriptors — binaries of mcp-capable
# agents — exactly what manifest.py emits as SHIM_AGENTS with every agent
# enabled (update-agent-keys.sh itself derives per-container from the keys
# dir, so there is no static list left to scrape).
SHIM="$(for f in "$REPO"/agents/*/agent.yml; do yq -r 'select(has("mcp")) | .binary' "$f"; done | LC_ALL=C sort | tr '\n' ' ')"
SHIM="${SHIM% }"
[ -n "$SHIM" ] || fail "no mcp-capable agents derived from agents/*/agent.yml (yq missing or descriptors moved?)"

d="$WORK/ck1"; mkdir -p "$d"; chmod 700 "$d"
MCP_GATEWAY_TOKEN=gwval GH_TOKEN=ghval SRC_C=ckey SRC_P=pkey
PES=$(printf 'MCP_GATEWAY_TOKEN\tMCP_GATEWAY_TOKEN\tgateway (run ./service.sh gateway once)\n')
AS=$(printf 'claude\tOBSIDIAN_ANNOTATED_KEY\tSRC_C\npi\tANNOTATED_WATCH_KEY\tSRC_P\n')
write_keyfiles "$d" "$SHIM" "$PES" "$AS" >/dev/null

assert_eq "claude.env = shared + its agent-scoped key" \
    $'MCP_GATEWAY_TOKEN=gwval\nGH_TOKEN=ghval\nOBSIDIAN_ANNOTATED_KEY=ckey' "$(cat "$d/claude.env")"
assert_eq "codex.env = shared only (no binding)" \
    $'MCP_GATEWAY_TOKEN=gwval\nGH_TOKEN=ghval' "$(cat "$d/codex.env")"
assert_eq "pi.env carries its watch key" \
    $'MCP_GATEWAY_TOKEN=gwval\nGH_TOKEN=ghval\nANNOTATED_WATCH_KEY=pkey' "$(cat "$d/pi.env")"
EXPECTED_ENV_BASENAMES=$(printf '%s\n' $SHIM | LC_ALL=C sort | tr '\n' ' ' | sed 's/ $//')
PRODUCED_ENV_BASENAMES=$(for f in "$d"/*.env; do
    basename "${f%.env}"
done | LC_ALL=C sort | tr '\n' ' ' | sed 's/ $//')
assert_eq "produced .env basename set matches SHIM" "$EXPECTED_ENV_BASENAMES" "$PRODUCED_ENV_BASENAMES"
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

# The git.hosts table (5th arg) lands in the shared block beside every
# variable it names — this is what git-credential-org resolves request hosts
# through, so the table itself AND each named variable (under its own name,
# exactly as written — no owner sanitisation) reach every shim agent.
d="$WORK/ck4"; mkdir -p "$d"; chmod 700 "$d"
GH_TOKEN=defval GH_TOKEN_fry=frytok GIT_HOST_TOKENS_FRYVAR=gamut
TABLE="github.com=GH_TOKEN git.example.test=GH_TOKEN_fry"
write_keyfiles "$d" "$SHIM" "" "" "$TABLE" >/dev/null
assert_eq "table + named variables land next to GH_TOKEN on each agent" \
    $'GIT_HOST_TOKENS=github.com=GH_TOKEN git.example.test=GH_TOKEN_fry\nGH_TOKEN=defval\nGH_TOKEN_fry=frytok' "$(cat "$d/codex.env")"
assert_eq "table fan-out reaches every shim agent" \
    $'GIT_HOST_TOKENS=github.com=GH_TOKEN git.example.test=GH_TOKEN_fry\nGH_TOKEN=defval\nGH_TOKEN_fry=frytok' "$(cat "$d/cursor-agent.env")"
unset GH_TOKEN GH_TOKEN_fry

# one variable serving several hosts collapses to one line (git_host_token_pairs
# and the keyfile writer dedupe by variable, so no duplicate rows appear).
d="$WORK/ck4d"; mkdir -p "$d"; chmod 700 "$d"
GH_TOKEN=defval SRC_SHARED=stok
TABLE="github.com=GH_TOKEN git.example.test=SRC_SHARED h2.test=SRC_SHARED"
write_keyfiles "$d" "codex" "" "" "$TABLE" >/dev/null
assert_eq "one shared variable written once, not per host" \
    $'GIT_HOST_TOKENS=github.com=GH_TOKEN git.example.test=SRC_SHARED h2.test=SRC_SHARED\nGH_TOKEN=defval\nSRC_SHARED=stok' "$(cat "$d/codex.env")"
unset GH_TOKEN SRC_SHARED

# OLD SPELLING, end to end at the key-file level: the git.token spelling is
# the CLI host's row github.com=GH_TOKEN_x, so GH_TOKEN equal to that
# secret's VALUE lands in every agent's key file — written once, not twice.
d="$WORK/ck7"; mkdir -p "$d"; chmod 700 "$d"
GH_TOKEN_x=old-form-value
GH_TOKEN="${GH_TOKEN_x}"   # what up.sh does with GIT_TOKEN_SOURCE=GH_TOKEN_x
write_keyfiles "$d" "$SHIM" "" "" "github.com=GH_TOKEN_x" >/dev/null
allhave=1; for a in $SHIM; do
    [ "$(cat "$d/$a.env")" = $'GIT_HOST_TOKENS=github.com=GH_TOKEN_x\nGH_TOKEN_x=old-form-value\nGH_TOKEN=old-form-value' ] || allhave=0
done
assert_eq "git.token: X lands GH_TOKEN=<X's value> in every agent key file" "1" "$allhave"
unset GH_TOKEN GH_TOKEN_x

# 5th arg omitted entirely still works (no table, no regression).
d="$WORK/ck4o"; mkdir -p "$d"; chmod 700 "$d"
GH_TOKEN=defval
write_keyfiles "$d" "codex" "" "" >/dev/null
assert_eq "5th arg omitted: no GIT_HOST_TOKENS line" "GH_TOKEN=defval" "$(cat "$d/codex.env")"
unset GH_TOKEN

# NEW FORM, end to end at the key-file level: a manifest that declares
# git.hosts.github.com.token: GH_TOKEN_x resolves GIT_TOKEN_SOURCE=GH_TOKEN_x
# (up.sh exports GH_TOKEN from it), so GH_TOKEN equal to that secret's VALUE
# lands in every agent's key file alongside the table row naming its variable.
d="$WORK/ck6"; mkdir -p "$d"; chmod 700 "$d"
GH_TOKEN_x=new-token-value
GH_TOKEN="${GH_TOKEN_x}"   # what up.sh does with GIT_TOKEN_SOURCE=GH_TOKEN_x
TABLE="github.com=GH_TOKEN_x"
write_keyfiles "$d" "$SHIM" "" "" "$TABLE" >/dev/null
allhave=1; for a in $SHIM; do
    [ "$(cat "$d/$a.env")" = $'GIT_HOST_TOKENS=github.com=GH_TOKEN_x\nGH_TOKEN_x=new-token-value\nGH_TOKEN=new-token-value' ] || allhave=0
done
assert_eq "git.hosts.github.com.token: GH_TOKEN becomes GH_TOKEN (table value) in every agent env file" "1" "$allhave"
unset GH_TOKEN GH_TOKEN_x

# no table at all → only GH_TOKEN (old minimal composition, no regression)
d="$WORK/ck5"; mkdir -p "$d"; chmod 700 "$d"
GH_TOKEN=defval
write_keyfiles "$d" "claude" "" "" "" >/dev/null
assert_eq "no table writes only GH_TOKEN (5th arg empty)" "GH_TOKEN=defval" "$(cat "$d/claude.env")"
unset GH_TOKEN

# ────────────────────────────────────────────────────────────────────────────
echo "── src/git-credential-org.sh ──"
# The in-container credential router: `get` on stdin (protocol/host/path),
# the request HOST resolved through GIT_HOST_TOKENS (the manifest's host→
# variable table). Listed host → that variable's value by indirect expansion;
# unlisted host → defer to gh when gh holds a login for it, else quit=1 with
# a stderr line naming git.hosts.<host>.token. Run the real script.
HELPER="$REPO/src/git-credential-org.sh"
# gh is never invoked unless the request reaches the fallback; whenever it
# CAN (a listed host with an empty row variable), it must never see the
# ambient home — real gh writes state (device ids, tokens) into it. Every
# helper call therefore runs with HOME and GH_CONFIG_DIR pointed at this
# suite's temp dirs; a caller's own env args (GH_CONFIG_DIR fixtures)
# come after and win.
ISO_CONF="$WORK/ghconf-iso"; mkdir -p "$ISO_CONF" "$WORK/gh-home"
gcred() { printf 'protocol=https\nhost=%s\npath=%s\n' "$1" "$2" \
    | env HOME="$WORK/gh-home" GH_CONFIG_DIR="$ISO_CONF" XDG_STATE_HOME="$WORK/gh-home/.local/state" "${@:3}" bash "$HELPER" get; }

# A listed host returns ITS OWN token, under its own variable name — no owner
# lookup, no sanitisation: the secrets.env variable exactly as written.
out=$(gcred git.example.test o/r.git GIT_HOST_TOKENS='git.example.test=SRC_FRY' SRC_FRY=frytok)
assert_contains "listed host → its token" "$out" "password=frytok"
assert_contains "listed host uses x-access-token username" "$out" "username=x-access-token"
assert_absent "listed host does not get another host's token" "$out" "password=xtok"

# A request for host A never returns host B's token: the walk stops at the
# matching row and only reads THAT row's variable.
out=$(gcred a.test o/r.git GIT_HOST_TOKENS='a.test=SRC_A b.test=SRC_B' SRC_A=atok SRC_B=btok)
assert_contains "request for host A returns host A's token" "$out" "password=atok"
assert_absent "…never host B's token" "$out" "password=btok"
out=$(gcred b.test o/r.git GIT_HOST_TOKENS='a.test=SRC_A b.test=SRC_B' SRC_A=atok SRC_B=btok)
assert_contains "request for host B returns host B's token" "$out" "password=btok"
assert_absent "…never host A's token" "$out" "password=atok"

# A host differing only by case, or carrying the explicit default port git
# passes for an https URL spelled with :443, still matches its row.
out=$(gcred Git.Example.Test o/r.git GIT_HOST_TOKENS='git.example.test=SRC_FRY' SRC_FRY=frytok)
assert_contains "mixed-case host still matches its row" "$out" "password=frytok"
out=$(gcred git.example.test:443 o/r.git GIT_HOST_TOKENS='git.example.test=SRC_FRY' SRC_FRY=frytok)
assert_contains "host:443 still matches the bare-host row" "$out" "password=frytok"
out=$(gcred git.example.test o/r.git GIT_HOST_TOKENS='Git.Example.Test:443=SRC_FRY' SRC_FRY=frytok)
assert_contains "a row spelled with case and :443 still matches a bare request" "$out" "password=frytok"

# The gh fallback serves a STORED gh login for EXACTLY the request host —
# nothing else. gh normalises *.github.com to github.com and honours
# GH_TOKEN/GITHUB_TOKEN (and their _ENTERPRISE variants) from its
# environment, so letting gh decide would leak a token to a host the table
# never named. The helper must decide from gh's own hosts file (exact host
# key) and strip the four token variables from gh's environment. The stub
# records every invocation plus any token variable that reaches it.
GH_CONF="$WORK/ghconf"; mkdir -p "$GH_CONF" "$WORK/ghbin"
printf 'github.com:\n    oauth_token: stored\n    user: someone\ngh.example.test:\n    oauth_token: stored\n    user: someone\n' > "$GH_CONF/hosts.yml"
GH_CALLS="$WORK/gh-calls.log"; : > "$GH_CALLS"
cat > "$WORK/ghbin/gh" <<MOCK
#!/bin/bash
echo "invoked \$*" >> "$GH_CALLS"
env | grep -E '^(GH_TOKEN|GITHUB_TOKEN|GH_ENTERPRISE_TOKEN|GITHUB_ENTERPRISE_TOKEN)=' >> "$GH_CALLS"
[ "\$1" = auth ] && { echo "username=human"; echo "password=humantok"; exit 0; }
exit 1
MOCK
chmod +x "$WORK/ghbin/gh"

# No hosts.yml at all: nothing stored, no fallback — quit=1, gh not invoked.
GH_EMPTY="$WORK/ghconf-empty"; mkdir -p "$GH_EMPTY"
out=$(printf 'protocol=https\nhost=gh.example.test\npath=o/r.git\n' | env -i PATH="$WORK/ghbin:$PATH" GH_CONFIG_DIR="$GH_EMPTY" GH_TOKEN=envleak GIT_HOST_TOKENS='git.example.test=SRC_FRY' SRC_FRY=frytok bash "$HELPER" get 2>"$WORK/cred-err"); rc=$?
assert_rc "no hosts.yml: clean exit" 0 "$rc"
assert_eq "no hosts.yml: tells git to quit" "quit=1" "$out"
assert_eq "no hosts.yml: gh never invoked" "" "$(cat "$GH_CALLS")"

# A host NOT exactly listed (gh normalises api.github.com onto github.com,
# and honours GH_TOKEN from the environment): quit=1 and gh is never
# invoked, even with GH_TOKEN set and github.com carrying a row.
out=$(printf 'protocol=https\nhost=api.github.com\npath=o/r.git\n' | env -i PATH="$WORK/ghbin:$PATH" GH_CONFIG_DIR="$GH_CONF" GH_TOKEN=envleak GIT_HOST_TOKENS='github.com=GH_TOKEN' bash "$HELPER" get 2>"$WORK/cred-err"); rc=$?
assert_rc "not-exactly-listed host, GH_TOKEN set: clean exit" 0 "$rc"
assert_eq "not-exactly-listed host: tells git to quit" "quit=1" "$out"
assert_eq "…gh never invoked (no gh-normalisation shortcut)" "" "$(cat "$GH_CALLS")"

# An unlisted host WITH a stored gh login (exact key in hosts.yml) defers to
# gh (the human lane), and gh's environment carries NONE of the four token
# variables — only the stored login can answer.
out=$(printf 'protocol=https\nhost=gh.example.test\npath=o/r.git\n' | env -i PATH="$WORK/ghbin:$PATH" GH_CONFIG_DIR="$GH_CONF" GH_TOKEN=envleak GITHUB_TOKEN=envleak2 GIT_HOST_TOKENS='git.example.test=SRC_FRY' SRC_FRY=frytok bash "$HELPER" get)
assert_contains "unlisted host with a stored gh login defers to gh" "$out" "password=humantok"
assert_absent "…and never leaks the table token to it" "$out" "password=frytok"
assert_eq "…exactly one gh invocation" "1" "$(grep -c 'invoked' "$GH_CALLS")"
assert_eq "…the stub sees none of the four token variables" \
    "" "$(grep -E '^(GH_TOKEN|GITHUB_TOKEN|GH_ENTERPRISE_TOKEN|GITHUB_ENTERPRISE_TOKEN)=' "$GH_CALLS")"

# A LISTED host whose variable is unset (or empty) takes the same path —
# gh answers only when it holds a stored login for EXACTLY that host.
calls_before=$(grep -c 'invoked' "$GH_CALLS")
out=$(printf 'protocol=https\nhost=git.example.test\npath=o/r.git\n' | env -i PATH="$WORK/ghbin:$PATH" GH_CONFIG_DIR="$GH_CONF" GIT_HOST_TOKENS='git.example.test=SRC_FRY' bash "$HELPER" get 2>"$WORK/cred-err"); rc=$?
assert_rc "listed host with unset token, no stored gh login: clean exit" 0 "$rc"
assert_eq "listed host with unset token, no stored gh login: quit=1" "quit=1" "$out"
assert_contains "…stderr names the unset variable" \
    "$(cat "$WORK/cred-err")" "git.hosts.git.example.test.token: 'SRC_FRY' is not set"
assert_eq "…gh is never invoked for it" "$calls_before" "$(grep -c 'invoked' "$GH_CALLS")"

# A deferral that FAILS (gh exits non-zero, or succeeds with no password=
# in its output) must not leave git to a prompt: quit=1 with the same
# stderr line naming git.hosts.<host>.token.
GH_FAIL="$WORK/ghconf-fail"; mkdir -p "$GH_FAIL"
printf 'github.com:\n    git_protocol: https\n' > "$GH_FAIL/hosts.yml"
cat > "$WORK/ghbin/gh" <<'MOCK'
#!/bin/bash
exit 1
MOCK
chmod +x "$WORK/ghbin/gh"
out=$(printf 'protocol=https\nhost=github.com\npath=o/r.git\n' | env -i PATH="$WORK/ghbin:$PATH" GH_CONFIG_DIR="$GH_FAIL" GIT_HOST_TOKENS='github.com=GH_TOKEN' bash "$HELPER" get 2>"$WORK/cred-err"); rc=$?
assert_rc "failing deferral: clean exit" 0 "$rc"
assert_eq "failing deferral: quit=1" "quit=1" "$out"
assert_contains "…stderr names the missing token" "$(cat "$WORK/cred-err")" "git.hosts.github.com.token"

cat > "$WORK/ghbin/gh" <<'MOCK'
#!/bin/bash
exit 0
MOCK
chmod +x "$WORK/ghbin/gh"
out=$(printf 'protocol=https\nhost=github.com\npath=o/r.git\n' | env -i PATH="$WORK/ghbin:$PATH" GH_CONFIG_DIR="$GH_FAIL" GIT_HOST_TOKENS='github.com=GH_TOKEN' bash "$HELPER" get 2>"$WORK/cred-err"); rc=$?
assert_rc "empty gh output: clean exit" 0 "$rc"
assert_eq "empty gh output: quit=1" "quit=1" "$out"
assert_contains "…stderr names the missing token" "$(cat "$WORK/cred-err")" "git.hosts.github.com.token"

# Restore the recording stub for the gh-invocation accounting below.
: > "$GH_CALLS"
cat > "$WORK/ghbin/gh" <<MOCK
#!/bin/bash
echo "invoked \$*" >> "$GH_CALLS"
env | grep -E '^(GH_TOKEN|GITHUB_TOKEN|GH_ENTERPRISE_TOKEN|GITHUB_ENTERPRISE_TOKEN)=' >> "$GH_CALLS"
[ "\$1" = auth ] && { echo "username=human"; echo "password=humantok"; exit 0; }
exit 1
MOCK
chmod +x "$WORK/ghbin/gh"

# Only the FIRST host= line routes: a request carrying a second host= line
# for a host with a stored gh login must be routed as the first host alone
# (a multi-line host must never reach the hosts.yml lookup as alternatives).
calls_before=$(grep -c 'invoked' "$GH_CALLS")
out=$(printf 'protocol=https\nhost=evil.example.test\nhost=github.com\npath=o/r.git\n' | env -i PATH="$WORK/ghbin:$PATH" GH_CONFIG_DIR="$GH_CONF" GIT_HOST_TOKENS='github.com=GH_TOKEN' GH_TOKEN=tableval bash "$HELPER" get 2>"$WORK/cred-err"); rc=$?
assert_rc "two host= lines: clean exit" 0 "$rc"
assert_eq "…routed as the first host only: quit=1" "quit=1" "$out"
assert_eq "…gh is never invoked" "$calls_before" "$(grep -c 'invoked' "$GH_CALLS")"

# A listed host whose variable is unset but which HAS a stored gh login
# defers instead of quitting.
out=$(printf 'protocol=https\nhost=gh.example.test\npath=o/r.git\n' | env -i PATH="$WORK/ghbin:$PATH" GH_CONFIG_DIR="$GH_CONF" GIT_HOST_TOKENS='gh.example.test=SRC_MISSING' bash "$HELPER" get)
assert_contains "listed host with unset token but a stored gh login defers" "$out" "password=humantok"

# A listed host with the token set never invokes gh.
calls_before=$(grep -c 'invoked' "$GH_CALLS")
out=$(gcred git.example.test o/r.git GIT_HOST_TOKENS='git.example.test=SRC_FRY' SRC_FRY=frytok)
assert_contains "listed host with a token never touches gh" "$out" "password=frytok"
assert_eq "…gh still not invoked" "$calls_before" "$(grep -c 'invoked' "$GH_CALLS")"
# hostnames are case-insensitive; git passes the URL's own spelling verbatim,
# including an explicit :443 — the same normalisation manifest.py applies.
out=$(gcred GitHub.COM nobody/x.git GIT_HOST_TOKENS='github.com=GH_TOKEN' GH_TOKEN=ghval)
assert_contains "mixed-case CLI host takes its table row" "$out" "password=ghval"
out=$(gcred github.com:443 nobody/x.git GIT_HOST_TOKENS='github.com=GH_TOKEN' GH_TOKEN=ghval)
assert_contains "host:443 takes the bare-host row" "$out" "password=ghval"

# A request with no host= line at all (git credential fill invoked by
# hand) has nothing to route — quit=1 so git stops instead of falling through
# to another helper or a prompt.
out=$(printf 'protocol=https\npath=acme/x.git\n' | env HOME="$WORK/gh-home" GH_CONFIG_DIR="$ISO_CONF" GIT_HOST_TOKENS='a.test=SRC_A' SRC_A=atok bash "$HELPER" get 2>"$WORK/cred-err"); rc=$?
assert_eq "no host= → quit=1" "quit=1" "$out"
assert_rc "no host= → clean exit" 0 "$rc"
assert_contains "no host= → stderr says why" "$(cat "$WORK/cred-err")" "git-credential-org: request carries no host= line — nothing to route"

# store/erase are no-ops (stateless helper) — no output, clean exit.
out=$(printf 'protocol=https\nhost=git.example.test\npath=o/r.git\n' | env HOME="$WORK/gh-home" GH_CONFIG_DIR="$ISO_CONF" GIT_HOST_TOKENS='git.example.test=SRC_FRY' SRC_FRY=frytok bash "$HELPER" store); rc=$?
assert_rc "store is a no-op (rc 0)" 0 "$rc"
assert_eq "store produces no output" "" "$out"

# Guards: the helper and the entrypoint must name NO forge for routing —
# every host resolves through the one table. Comment lines may explain
# history; no executable line may name one.
forge_lines() { grep -vn '^[[:space:]]*#' "$1" | grep -v "^[0-9]*: *#" | grep 'github\.com' || true; }
helper_leak=$(grep -v '^[[:space:]]*#' "$REPO/src/git-credential-org.sh" | grep -c 'github\.com' || true)
assert_eq "helper: no github.com outside comment lines" "0" "$helper_leak"
ep_leak=$(grep -v '^[[:space:]]*#' "$REPO/src/entrypoint.sh" | grep -c 'github\.com' || true)
assert_eq "entrypoint: no github.com outside comment lines" "0" "$ep_leak"

# ────────────────────────────────────────────────────────────────────────────
echo "── entrypoint.sh: per-host credential-helper install ──"
# entrypoint.sh installs git-credential-org as the helper for every origin in
# GIT_CREDENTIAL_HOSTS (no host hard-coded in the loop). VS Code's
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

# The fix: reset(empty)+add, verbatim from entrypoint.sh (minus `su … coder`),
# looped over GIT_CREDENTIAL_HOSTS (the git.hosts table's hosts plus every
# https:// origin in repos:; newline-separated — word-split on purpose).
GIT_CREDENTIAL_HOSTS=$'https://github.com\nhttps://git.example.test\n'
install_helper() {
  for origin in $GIT_CREDENTIAL_HOSTS; do
    gc --unset-all "credential.$origin.helper" 2>/dev/null || true
    gc --add "credential.$origin.helper" ''
    gc --add "credential.$origin.helper" /usr/local/bin/git-credential-org
  done
}
seed_home; install_helper; rc=$?
assert_rc "reset+add succeeds despite the duplicated pre-seed" 0 "$rc"
install_helper; rc=$?   # container restart / re-attach runs it again
assert_rc "reset+add is idempotent on re-run" 0 "$rc"

# The payoff: the effective helper chain git would use for a github.com URL is
# ONLY the router — the empty reset dropped the inherited desktop bridge, so no
# human-login leak. --get-urlmatch merges generic+host-specific honouring resets.
eff=$(HOME="$EH" GIT_CONFIG_SYSTEM="$SYSCFG" git config --get-urlmatch credential.helper https://github.com/dmtrio/x.git)
assert_eq "router is the sole effective CLI-host helper (bridge cleared)" \
    "/usr/local/bin/git-credential-org" "$eff"
eff=$(HOME="$EH" GIT_CONFIG_SYSTEM="$SYSCFG" git config --get-urlmatch credential.helper https://git.example.test/Emergence/filebrowser.git)
assert_eq "router is the sole effective helper for a manifest gitea origin" \
    "/usr/local/bin/git-credential-org" "$eff"
eff=$(HOME="$EH" GIT_CONFIG_SYSTEM="$SYSCFG" git config --get-urlmatch credential.helper https://other.example.test/x/y.git)
assert_eq "an origin NOT in GIT_CREDENTIAL_HOSTS keeps only the desktop bridge (router not installed)" \
    "!desktop-bridge" "$eff"

# Drift pin: entrypoint.sh must keep the idempotent idiom. If anyone reverts to a
# plain `git config … helper <value>` set, these fail loudly (mirrors the
# up.sh/plugins.test.sh drift pins).
EP=$(cat "$REPO/src/entrypoint.sh")
assert_contains "entrypoint loops every GIT_CREDENTIAL_HOSTS origin (no hard-coded host)" \
    "$EP" 'for origin in $GIT_CREDENTIAL_HOSTS; do'
assert_contains "entrypoint resets the helper list (--unset-all)" \
    "$EP" "--unset-all credential.'\$origin'.helper"
assert_contains "entrypoint adds an empty reset before the router" \
    "$EP" "--add credential.'\$origin'.helper ''"
assert_contains "entrypoint adds the router via --add (not a plain set)" \
    "$EP" "--add credential.'\$origin'.helper /usr/local/bin/git-credential-org"
# The loop expands unquoted with globbing on; values are manifest-validated,
# but word-splitting must not glob. Check set -f sits directly before the for.
if grep -B1 '^for origin in \$GIT_CREDENTIAL_HOSTS; do' "$REPO/src/entrypoint.sh" \
    | head -1 | grep -q '^set -f'; then
  pass "entrypoint loop is glob-safe (set -f)"
else
  fail "entrypoint loop is glob-safe (set -f)"
fi
grep -q 'GIT_CREDENTIAL_HOSTS=\${GIT_CREDENTIAL_HOSTS:-}' "$REPO/compose/docker-compose.local.yml" \
    && pass "compose passes GIT_CREDENTIAL_HOSTS into the container" \
    || fail "compose no longer passes GIT_CREDENTIAL_HOSTS (entrypoint would install no helper at all)"
grep -q 'GIT_CREDENTIAL_HOSTS="\$GIT_CREDENTIAL_HOSTS"' "$REPO/up.sh" \
    && pass "up.sh hands GIT_CREDENTIAL_HOSTS to compose" \
    || fail "up.sh no longer hands GIT_CREDENTIAL_HOSTS to compose"
grep -qF 'git.hosts.$_h.token names a variable set in secrets.env' "$REPO/up.sh" \
    && pass "up.sh warns with the non-github clone-failure message" \
    || fail "up.sh missing the non-github clone-failure warning text"
grep -qF 'git_url_split "$RURL"' "$REPO/up.sh" \
    && pass "up.sh derives the host/path via git_url_split" \
    || fail "up.sh derives the host/path via git_url_split"
grep -qF 'case "$_h" in github.com|github.com:443)' "$REPO/up.sh" \
    && pass "up.sh clone warning matches on host, not URL" \
    || fail "up.sh clone warning matches on host, not URL"
grep -qF 'REPO_OWNER="${_p%%/*}"' "$REPO/up.sh" \
    && pass "up.sh derives REPO_OWNER from the shared host/path split" \
    || fail "up.sh derives REPO_OWNER from the shared host/path split"
grep -qF 'write_keyfiles "$KEYS_PATH" "$SHIM_AGENTS" "$PLUGIN_ENV_SECRETS" "$AGENT_SECRETS" "$GIT_HOST_TOKENS"' "$REPO/up.sh" \
    && pass "up.sh passes GIT_HOST_TOKENS to write_keyfiles" \
    || fail "up.sh no longer passes GIT_HOST_TOKENS to write_keyfiles"
grep -qF 'git_host_token_pairs "$GIT_HOST_TOKENS"' "$REPO/up.sh" \
    && pass "up.sh builds the bootstrap clone env via git_host_token_pairs" \
    || fail "up.sh no longer forwards GIT_HOST_TOKENS + row variables to the bootstrap clone"
grep -qF '"${CLONE_ENV[@]}"' "$REPO/up.sh" \
    && pass "up.sh passes the clone env as an array (pairs stay single argv words)" \
    || fail "up.sh no longer expands CLONE_ENV as an array (space-separated pairs would word-split into separate -e args)"
grep -qF 'no git.hosts.' "$REPO/src/git_notices.sh" \
    && pass "git_notices.sh warns about a repo host with no git.hosts row" \
    || fail "git_notices.sh missing the no-row up-time notice"
grep -qF 'git_orgs_host_notice' "$REPO/up.sh" \
    && pass "up.sh calls the git.orgs shared-host notice" \
    || fail "up.sh no longer calls git_orgs_host_notice"
grep -qF 'git host is not in capabilities.egress' "$REPO/src/git_notices.sh" \
    && pass "git_notices.sh warns when a bound git host is missing from capabilities.egress" \
    || fail "git_notices.sh missing the egress-coverage notice for a bound git host"

# ────────────────────────────────────────────────────────────────────────────
echo "── src/git_notices.sh ──"
# shellcheck disable=SC1091
. "$REPO/src/git_notices.sh"   # defines git_host_notices + git_egress_notices, no side effects

out=$(git_host_notices $'a\thttps://h.test/a/x.git\nb\thttps://h.test/a/y.git\n' '')
assert_eq "git_host_notices: one notice per host, not per repo" \
    "1" "$(printf '%s\n' "$out" | grep -c '^  note:')"
assert_contains "git_host_notices: notes the host, not host/owner" "$out" "h.test: no git.hosts.h.test.token"

# git_orgs_host_notice: when a git.orgs entry supplies a host's table row and
# repos: lists ANOTHER owner on that host, one line says that host's token
# now serves every owner on it, naming git.hosts.<host>.token as the way to
# state it. Only hosts a git.orgs entry actually routed to are in scope
# (GIT_ORG_ROUTED_HOSTS).
out=$(git_orgs_host_notice $'a\thttps://h.test/acme/x.git\nb\thttps://h.test/other/y.git\n' \
    'github.com=GH_TOKEN h.test=SRC_ACME' 'h.test')
assert_contains "git_orgs_host_notice: two owners on one host → one notice" "$out" \
    "note: h.test: SRC_ACME now serves every owner on this host"
assert_contains "…naming git.hosts.<host>.token as the way to state it" "$out" \
    "state it explicitly with git.hosts.h.test.token: SRC_ACME"
# FALSE case: the host's row came from git.token and the git.orgs entry is
# for ANOTHER host — the notice must not attribute the row to git.orgs.
out=$(git_orgs_host_notice $'a\thttps://h.test/acme/x.git\nb\thttps://h.test/other/y.git\n' \
    'github.com=GH_TOKEN h.test=SRC_A' 'git.other.test')
assert_eq "git_orgs_host_notice: a host whose row came from git.token stays silent" "" "$out"
out=$(git_orgs_host_notice $'a\thttps://h.test/acme/x.git\n' 'h.test=SRC_ACME' 'h.test')
assert_eq "git_orgs_host_notice: a single-owner host is silent" "" "$out"
out=$(git_orgs_host_notice $'a\thttps://h.test/acme/x.git\nb\thttps://h.test/other/y.git\n' \
    'github.com=GH_TOKEN' 'github.com')
assert_eq "git_orgs_host_notice: a row-less host is out of scope" "" "$out"
out=$(git_orgs_host_notice $'a\thttps://h.test/acme/x.git\nb\thttps://h.test/other/y.git\n' \
    'h.test=SRC_ACME' '')
assert_eq "git_orgs_host_notice: no git.orgs entries → silent" "" "$out"
out=$(git_orgs_host_notice $'a\thttps://h.test/Acme/x.git\nb\thttps://h.test/acme/y.git\n' \
    'h.test=SRC_ACME' 'h.test')
assert_eq "git_orgs_host_notice: same owner twice on one host is one owner, silent" "" "$out"
out=$(git_orgs_host_notice $'a\tgit@h.test:acme/x.git\nb\thttps://h.test/other/y.git\n' \
    'h.test=SRC_ACME' 'h.test')
assert_contains "git_orgs_host_notice: an scp-only owner still leaves one https owner… silent" "" \
    "$(git_orgs_host_notice $'a\tgit@h.test:acme/x.git\n' 'h.test=SRC_ACME' 'h.test')"

out=$(git_host_notices $'a\thttps://h.test/a/x.git\nb\thttps://h2.test/b/y.git\n' 'h.test=SRC_A')
assert_eq "git_host_notices: a host WITH a table row gets no notice" \
    "1" "$(printf '%s\n' "$out" | grep -c '^  note:')"
assert_contains "…and the row-less host is the one noticed" "$out" "h2.test: no git.hosts.h2.test.token"

# Host normalisation: a row spelled with case and the explicit default port
# still covers the request host, and a :port host row only covers :port.
out=$(git_host_notices $'a\thttps://Git.Example.Test/a/x.git\n' 'Git.Example.Test:443=SRC_A')
assert_eq "git_host_notices: a :443 row covers the bare host" "" "$out"
out=$(git_host_notices $'a\thttps://Git.Example.Test:3000/a/x.git\n' 'git.example.test=SRC_A')
assert_contains "git_host_notices: a :3000 repo is NOT covered by the bare-host row" "$out" "git.example.test:3000"

# An ssh:// repo never gets a notice (it takes no HTTP credential at all),
# and the CLI host keeps its implicit row, so it never notices either.
out=$(git_host_notices $'a\tssh://git@h.test/a/x.git\n' '')
assert_eq "git_host_notices: an ssh:// repo never gets a notice" "" "$out"
out=$(git_host_notices $'a\thttps://github.com/acme/x.git\n' 'github.com=GH_TOKEN')
assert_eq "git_host_notices: the CLI host with its row gets no notice" "" "$out"

# End to end through the real derive: the notice list drops the
# base-allowlisted CLI host, so the up-time egress notice is silent for it
# even with empty capabilities.egress (it would have fired on every bottle).
eval "$(printf '{"repos":["https://github.com/x/y.git","https://git.example.test/o/r.git"]}\n---agents---\na\t{"binary":"a","install":"x"}\n' \
    | PRESENT_SECRET_VARS="GH_TOKEN" SECRETS_FILE=/sec/secrets.env \
      GIT_NAME_DEFAULT="" GIT_EMAIL_DEFAULT="" NTFY_URL="" NTFY_TOPIC="" \
      python3 "$REPO/src/manifest.py" --derive)"
assert_eq "derive drops the base-allowlisted CLI host from the notice list" \
    $'https://git.example.test\n' "$GIT_EGRESS_NOTICE_HOSTS"
# A github-only bottle: the derived list is empty, so the notice is silent —
# it used to fire a false note on every such bottle.
eval "$(printf '{"repos":["https://github.com/x/y.git"]}\n---agents---\na\t{"binary":"a","install":"x"}\n' \
    | PRESENT_SECRET_VARS="GH_TOKEN" SECRETS_FILE=/sec/secrets.env \
      GIT_NAME_DEFAULT="" GIT_EMAIL_DEFAULT="" NTFY_URL="" NTFY_TOPIC="" \
      python3 "$REPO/src/manifest.py" --derive)"
assert_eq "a github-only bottle derives an empty notice list" "" "$GIT_EGRESS_NOTICE_HOSTS"
out=$(git_egress_notices "$GIT_EGRESS_NOTICE_HOSTS" '' '')
assert_eq "git_egress_notices: the base-allowlisted CLI host is silent with empty egress" "" "$out"
out=$(git_egress_notices $'https://git.example.test\n' "git.example.test" '')
assert_eq "git_egress_notices: exact host match in EGRESS, no notice" "" "$out"
out=$(git_egress_notices $'https://git.example.test\n' "example.test" '')
assert_eq "git_egress_notices: a parent domain covers the host, no notice" "" "$out"
out=$(git_egress_notices $'https://git.example.test\n' "other.test" '')
assert_contains "git_egress_notices: an unrelated zone still notices" "$out" "git.example.test"
out=$(git_egress_notices $'https://git.example.test\n' "" '')
assert_contains "git_egress_notices: an empty EGRESS still notices" "$out" "git.example.test"
out=$(git_egress_notices $'https://192.168.1.10\n' '' '192.168.1.0/24')
assert_eq "git_egress_notices: an IP-literal host with egress_cidrs set, exactly one note" \
    "1" "$(printf '%s\n' "$out" | grep -c '^  note:')"
assert_contains "git_egress_notices: IP-literal host note mentions egress_cidrs and covers it" \
    "$out" "unless egress_cidrs (192.168.1.0/24) covers it"
out=$(git_egress_notices $'https://192.168.1.10\n' '' '')
assert_contains "git_egress_notices: an IP-literal host with no CIDR grant still notices" "$out" "192.168.1.10"
out=$(git_egress_notices $'https://git.example.test\n' "Example.Test" '')
assert_eq "git_egress_notices: zone match is case-insensitive" "" "$out"
out=$(git_egress_notices $'https://git.example.test\n' '' '10.0.0.0/8')
assert_eq "git_egress_notices: a DNS-named host with a CIDR grant, exactly one note" \
    "1" "$(printf '%s\n' "$out" | grep -c '^  note:')"
assert_contains "git_egress_notices: DNS-named host note mentions egress_cidrs" "$out" "egress_cidrs (10.0.0.0/8)"

grep -qF '. "$SCRIPT_DIR/src/git_notices.sh"' "$REPO/up.sh" \
    && pass "up.sh sources src/git_notices.sh" \
    || fail "up.sh no longer sources src/git_notices.sh"
grep -qF 'git_host_notices "$REPOS" "$GIT_HOST_TOKENS"' "$REPO/up.sh" \
    && pass "up.sh calls git_host_notices" \
    || fail "up.sh no longer calls git_host_notices"
grep -qF 'git_egress_notices "$GIT_EGRESS_NOTICE_HOSTS" "$EGRESS" "$EGRESS_CIDRS"' "$REPO/up.sh" \
    && pass "up.sh calls git_egress_notices with the notice-host list" \
    || fail "up.sh no longer calls git_egress_notices"

# The up-time notice loop's scheme guard must be case-insensitive and
# admit only https:// (scp-style/ssh:// take no HTTP credential at all).
grep -qF '[ "$_rscheme" = https ] || continue' "$REPO/src/git_notices.sh" \
    && pass "git_notices.sh notice loop's scheme guard is the case-insensitive https check" \
    || fail "git_notices.sh notice loop's scheme guard is the case-insensitive https check"
! grep -qF '*://*) ;;' "$REPO/src/git_notices.sh" \
    && pass "git_notices.sh notice loop's old *://*) guard is gone" \
    || fail "git_notices.sh notice loop's old *://*) guard is still present"

# Functional test of git_url_split itself (defined in src/git_notices.sh,
# sourced above) — the same host/path derivation up.sh's clone loop now
# calls, so a rewrite of the case arms is caught by behavior, not just by
# the drift-pin greps above.
git_url_split 'https://github.com/o/r.git'
assert_eq "git_url_split: plain github.com URL host" "github.com" "$_h"
git_url_split 'https://bot@github.com:443/o/r.git'
assert_eq "git_url_split: userinfo@github.com:443 URL host" "github.com:443" "$_h"
assert_eq "git_url_split: userinfo@github.com:443 URL owner" "o" "${_p%%/*}"
git_url_split 'https://gitea.example.test/org/a@github.com/b.git'
assert_eq "git_url_split: an '@' in the path before a real userinfo does not confuse host" \
    "gitea.example.test" "$_h"
assert_eq "git_url_split: an '@' in the path before a real userinfo does not confuse owner" \
    "org" "${_p%%/*}"
git_url_split 'https://github.com.evil.test/o/r.git'
assert_eq "git_url_split: github.com as a suffix of another host is NOT github.com" \
    "github.com.evil.test" "$_h"
git_url_split 'git@github.com:dmtrio/x.git'
assert_eq "git_url_split: scp-style github host" "github.com" "$_h"
assert_eq "git_url_split: scp-style github owner" "dmtrio" "${_p%%/*}"
git_url_split 'git@gitea.example.test:org/x.git'
assert_eq "git_url_split: scp-style non-github host" "gitea.example.test" "$_h"
git_url_split 'ssh://git@github.com/o/r.git'
assert_eq "git_url_split: ssh:// URL owner" "o" "${_p%%/*}"
git_url_split 'https://GitHub.com/o/r.git'
assert_eq "git_url_split: host is lowercased (the case the mirror missed)" "github.com" "$_h"

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


# A failing COMMAND in ./.env (as opposed to an explicit `exit`, which would
# terminate the shell directly) is what common.sh's set +e guard converts into
# a loud exit 1 instead of a silent abort under the caller's set -e.
printf 'false\n' > "$cfg/.env"
out=$(env -i bash -c 'set -e; . "$1"' _ "$cfg/src/common.sh" 2>&1); rc=$?
assert_rc "broken ./.env aborts with exit 1" 1 "$rc"
assert_contains "broken ./.env reports the failure" "$out" "./.env exited non-zero"
rm -f "$cfg/.env"

# BOTTLES_PATH resolution, in order: BOTTLES_PATH → CONTAINERS_PATH (deprecated)
# → $BASE_PATH/bottles → $BASE_PATH/containers (deprecated) → the repo's bottles/.
# All five branches are pinned: a wrong pick here reads someone's stale manifest
# directory and applies it, which looks like a successful run.
cpath() { env -i DJINN_HOME="${1-}" BOTTLES_PATH="${2-}" CONTAINERS_PATH="${3-}" bash -c '. "$1"; echo "$BOTTLES_PATH"' _ "$cfg/src/common.sh" 2>/dev/null; }
cerr()  { env -i DJINN_HOME="${1-}" BOTTLES_PATH="${2-}" CONTAINERS_PATH="${3-}" bash -c '. "$1"; echo "$BOTTLES_PATH" >/dev/null' _ "$cfg/src/common.sh" 2>&1; }
assert_eq "default BOTTLES_PATH is the repo's bottles/" "$cfg/bottles" "$(cpath '' '')"
dah="$WORK/dah-cp"; mkdir -p "$dah/bottles"
assert_eq "\$BASE_PATH/bottles wins when it exists" "$dah/bottles" "$(cpath "$dah" '')"
assert_eq "BOTTLES_PATH env override wins over everything" "/my/private/bottles" "$(cpath "$dah" /my/private/bottles)"

# Deprecated spellings stay honored so a live ./.env keeps working — each warns.
assert_eq "CONTAINERS_PATH is still honored" "/legacy/manifests" "$(cpath '' '' /legacy/manifests)"
assert_contains "CONTAINERS_PATH warns" "$(cerr '' '' /legacy/manifests)" "CONTAINERS_PATH is deprecated"
assert_eq "BOTTLES_PATH wins over CONTAINERS_PATH" "/new/bottles" "$(cpath '' /new/bottles /legacy/manifests)"
assert_eq "no warning when only BOTTLES_PATH is set" "" "$(cerr '' /new/bottles '')"
dac="$WORK/dac-cp"; mkdir -p "$dac/containers"
assert_eq "\$BASE_PATH/containers is still honored" "$dac/containers" "$(cpath "$dac" '')"
assert_contains "\$BASE_PATH/containers warns" "$(cerr "$dac" '')" "is deprecated"
both="$WORK/both-cp"; mkdir -p "$both/bottles" "$both/containers"
assert_eq "\$BASE_PATH/bottles wins over the legacy containers/" "$both/bottles" "$(cpath "$both" '')"
assert_eq "no warning when bottles/ wins" "" "$(cerr "$both" '')"

# BOTTLES_BUNDLED gates whether up.sh may `git pull` the bottles dir. It
# must be 1 for exactly the in-repo fallback: reporting 0 there would have a
# fresh clone try to pull its own checkout (i.e. pull brassbottle).
cbundled() { env -i DJINN_HOME="${1-}" BOTTLES_PATH="${2-}" CONTAINERS_PATH="${3-}" bash -c '. "$1"; echo "$BOTTLES_BUNDLED"' _ "$cfg/src/common.sh" 2>/dev/null; }
assert_eq "bundled flag set for the repo's own bottles/" "1" "$(cbundled '' '')"
assert_eq "bundled flag clear for \$BASE_PATH/bottles" "0" "$(cbundled "$dah" '')"
assert_eq "bundled flag clear for a BOTTLES_PATH override" "0" "$(cbundled "$dah" /my/private/bottles)"
assert_eq "bundled flag clear for a deprecated CONTAINERS_PATH" "0" "$(cbundled '' '' /legacy/manifests)"

# require_python3: prefers the SYSTEM interpreter, proves it runs, and sets
# $PYTHON3 for the caller — a broken pyenv/brew shim on PATH must not win
# silently. env -i so no ambient PYTHON3/PATH from this shell leaks in.
rp3_out() { env -i PYTHON3="${1-}" bash -c '. "$1"; require_python3 && echo "PYTHON3=$PYTHON3"' _ "$cfg/src/common.sh"; }
rp3_rc()  { env -i PYTHON3="${1-}" bash -c '. "$1"; require_python3 >/dev/null 2>&1; echo $?' _ "$cfg/src/common.sh"; }
rp3_err() { env -i PYTHON3="${1-}" bash -c '. "$1"; require_python3' _ "$cfg/src/common.sh" 2>&1 1>/dev/null; }

assert_eq "PYTHON3=/nonexistent → require_python3 fails (rc 1)" "1" "$(rp3_rc /nonexistent/python3)"
assert_contains "…with the broken-interpreter diagnostic" "$(rp3_err /nonexistent/python3)" \
    "no working python3 (tried /usr/bin/python3 and PATH"
assert_eq "empty PYTHON3 → falls back to a working interpreter (rc 0)" "0" "$(rp3_rc '')"
assert_contains "…and sets \$PYTHON3 to the one it found" "$(rp3_out '')" "PYTHON3=/"

# ────────────────────────────────────────────────────────────────────────────
echo "── allow-egress.sh ──"
run_ae() { ( cd "$SBOX" && env BOTTLES_PATH="$WORK/no-such-manifests" \
    MOCK_EXISTING_CONTAINERS="${MOCK_EXISTING_CONTAINERS-djinn-mycontainer}" \
    PATH="$WORK/aebin:$PATH" bash bin/allow-egress.sh "$@" ) 2>&1; }
# docker mock: `inspect [-f fmt] <name>` only "succeeds" (State.Running=false,
# so the live-apply path is skipped) for a name in the space-separated
# MOCK_EXISTING_CONTAINERS allowlist (env var the test sets) — everything else
# reports not-found. Tightened from "inspect always succeeds" so the
# djinn- prefix resolution in allow-egress.sh is actually
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

# notify_egress_daemon: skip when daemon invoked the script; curl /decide for manual runs.
echo "── allow-egress notify daemon ──"
CURL_LOG="$WORK/curl.log"
: > "$CURL_LOG"
mkdir -p "$WORK/curlbin"
cat > "$WORK/curlbin/curl" <<MOCK
#!/bin/bash
echo "\$*" >> "$CURL_LOG"
exit 0
MOCK
chmod +x "$WORK/curlbin/curl"

DJINN_HOME="$WORK/djhome"
RUN_PATH="$DJINN_HOME/run"
mkdir -p "$RUN_PATH/egress"
echo "notify-test-token" > "$RUN_PATH/egress/operator.token"

cat > "$WORK/aebin/docker" <<'MOCK'
#!/bin/bash
case "$1" in
    inspect)
        if [ "$2" = "-f" ]; then
            name="$4"
            fmt="$3"
        else
            name="$2"
            fmt=""
        fi
        for c in $MOCK_EXISTING_CONTAINERS; do
            if [ "$c" = "$name" ]; then
                if [ "$fmt" = "{{.State.Running}}" ]; then
                    echo true
                else
                    echo false
                fi
                exit 0
            fi
        done
        exit 1
        ;;
    exec)
        if [ "$3" = "test" ] && [ "$4" = "-f" ] && [ "$5" = "/etc/dnsmasq.conf" ]; then
            exit 0
        fi
        if [ "$2" = "-i" ]; then
            exit 0
        fi
        exit 0
        ;;
    ps) exit 0 ;;
    *) exit 0 ;;
esac
MOCK
chmod +x "$WORK/aebin/docker"

run_ae_notify() {
    ( cd "$SBOX" && env DJINN_HOME="$DJINN_HOME" BOTTLES_PATH="$WORK/no-such-manifests" \
        MOCK_EXISTING_CONTAINERS="${MOCK_EXISTING_CONTAINERS-djinn-mycontainer}" \
        EGRESS_BROKER_PORT="${EGRESS_BROKER_PORT:-8816}" \
        PATH="$WORK/curlbin:$WORK/aebin:$PATH" bash bin/allow-egress.sh "$@" ) 2>&1
}

: > "$CURL_LOG"
out=$(run_ae_notify mycontainer notify.example.com --save none); rc=$?
assert_rc "notify path with running container rc 0" 0 "$rc"
assert_contains "notify path echoes domain" "$out" "notify.example.com"
if grep -q '/decide' "$CURL_LOG"; then
    pass "manual allow notifies egress daemon"
else
    fail "manual allow should POST /decide to egress daemon"
fi
if grep -q -- '--max-time' "$CURL_LOG"; then
    pass "notify curls use --max-time"
else
    fail "notify curls missing --max-time"
fi

: > "$CURL_LOG"
out=$(DJINN_EGRESS_SKIP_NOTIFY=1 run_ae_notify mycontainer skip.example.com --save none); rc=$?
assert_rc "daemon-invoked allow skips notify rc 0" 0 "$rc"
if grep -q '/decide' "$CURL_LOG"; then
    fail "daemon-invoked allow should not POST /decide"
else
    pass "daemon-invoked allow skips notify"
fi

# finding (PR #85 review, daemon endpoint): notify_egress_daemon must ask the
# daemon where it actually bound (daemon.json via --print-endpoint) rather
# than assuming the default port — a live daemon on a non-default --host/
# --port used to be silently treated as unreachable. Stub egress_broker_host.py
# so --print-endpoint reports a distinctive, non-default endpoint; confirm the
# notify POST actually targets it when python3 works.
cat > "$SBOX/src/egress_broker_host.py" <<'PYEOF'
#!/usr/bin/env python3
import sys
if "--print-endpoint" in sys.argv:
    print("http://127.0.0.1:19191")
    sys.exit(0)
sys.exit(1)
PYEOF

: > "$CURL_LOG"
out=$(run_ae_notify mycontainer printed-endpoint.example.com --save none); rc=$?
assert_rc "notify with printed endpoint rc 0" 0 "$rc"
if grep -q '127.0.0.1:19191/decide' "$CURL_LOG"; then
    pass "notify_egress_daemon targets the printed --print-endpoint address"
else
    fail "notify_egress_daemon should target the printed --print-endpoint address"
fi

# ...but exit 3 from --print-endpoint means "no live daemon.json, this is the
# hardcoded default": the printed URL must be discarded so an operator-set
# EGRESS_BROKER_PORT still wins, as it did before endpoint discovery existed.
cat > "$SBOX/src/egress_broker_host.py" <<'PYEOF'
#!/usr/bin/env python3
import sys
if "--print-endpoint" in sys.argv:
    print("http://127.0.0.1:8816")
    sys.exit(3)
sys.exit(1)
PYEOF

: > "$CURL_LOG"
out=$(EGRESS_BROKER_PORT=19292 run_ae_notify mycontainer stale-endpoint.example.com --save none); rc=$?
assert_rc "notify with stale endpoint (exit 3) rc 0" 0 "$rc"
if grep -q '127.0.0.1:19292/decide' "$CURL_LOG"; then
    pass "notify_egress_daemon honours EGRESS_BROKER_PORT when --print-endpoint exits 3"
else
    fail "notify_egress_daemon should fall back to EGRESS_BROKER_PORT on --print-endpoint exit 3"
fi

# Same call, but python3 is broken: notify_egress_daemon must not re-probe
# require_python3 (it reuses HAVE_PYTHON3 from the --check probe earlier in
# the script) and must fall back to the env/default guess rather than ever
# reaching the (stubbed) egress_broker_host.py above.
run_ae_notify_no_python() {
    ( cd "$SBOX" && env DJINN_HOME="$DJINN_HOME" BOTTLES_PATH="$WORK/no-such-manifests" \
        MOCK_EXISTING_CONTAINERS="${MOCK_EXISTING_CONTAINERS-djinn-mycontainer}" \
        EGRESS_BROKER_PORT="${EGRESS_BROKER_PORT:-8816}" \
        PYTHON3=/nonexistent/python3 \
        PATH="$WORK/curlbin:$WORK/aebin:$PATH" bash bin/allow-egress.sh "$@" ) 2>&1
}

: > "$CURL_LOG"
out=$(run_ae_notify_no_python mycontainer no-python-fallback.example.com --save none); rc=$?
assert_rc "notify with broken python3 still succeeds (env fallback) rc 0" 0 "$rc"
if grep -q '127.0.0.1:8816/decide' "$CURL_LOG"; then
    pass "notify_egress_daemon falls back to the env/default endpoint when python3 is broken"
else
    fail "notify_egress_daemon should fall back to the env/default endpoint when python3 is broken"
fi
if grep -q '19191' "$CURL_LOG"; then
    fail "notify_egress_daemon must not reach the printed-endpoint stub when python3 is broken"
else
    pass "notify_egress_daemon does not call the printed-endpoint stub when python3 is broken"
fi

# An unknown container → a clear error naming the attempt, plus a ps -a hint.
MOCK_EXISTING_CONTAINERS=""
out=$(run_ae ghost cdn.playwright.dev --save none 2>&1); rc=$?
assert_rc "unknown container → rc 1" 1 "$rc"
assert_contains "error names the container it looked for" "$out" "no container named 'djinn-ghost'"
assert_contains "not-found hint lists djinn- containers" "$out" "Existing djinn containers:"
unset MOCK_EXISTING_CONTAINERS

# ────────────────────────────────────────────────────────────────────────────
echo "── allow-egress.sh --check: deny-list probe invocation ──"
# finding #10: allow-egress.sh's --check line is bash glue ONLY now — all
# matching logic AND the operator-facing output (the three-arm coverage:
# covered / not covered / corrupt file) moved into
# src/egress_denylist.py's own --check probe and its Python unit tests
# (tests/test_egress_denylist.py). This is bash's whole remaining
# responsibility: invoke it once with every domain, and turn a nonzero
# exit (a genuine crash — --check itself exits 0 for every case it
# handles) into one named failure line. Stub the script in the sandbox to
# prove exactly that, without a real denylist.json. CDD_ROOT resolves to
# $SBOX (common.sh's dirname-of-self/.. from $SBOX/src/common.sh), so
# allow-egress.sh finds this stub at "$CDD_ROOT/src/egress_denylist.py"
# exactly like the real one.
cat > "$SBOX/src/egress_denylist.py" <<'PYEOF'
#!/usr/bin/env python3
import sys
from pathlib import Path
Path(__file__).with_name("check_argv.log").write_text(" ".join(sys.argv[1:]))
if any("crashy" in a for a in sys.argv[1:]):
    print("boom: simulated crash", file=sys.stderr)
    sys.exit(127)
sys.exit(0)
PYEOF

out=$(run_ae mycontainer domain-a.example.com domain-b.example.com --save none); rc=$?
assert_rc "--check invocation still succeeds overall" 0 "$rc"
argv="$(cat "$SBOX/src/check_argv.log")"
assert_eq "--check is invoked once with the bottle and every domain" \
    "--check mycontainer domain-a.example.com domain-b.example.com" "$argv"

out=$(run_ae mycontainer crashy.example.com --save none); rc=$?
assert_rc "--check crash (genuine failure) still succeeds overall" 0 "$rc"
assert_contains "--check nonzero exit prints the failure line" "$out" \
    "⚠ deny-list check failed (exit 127)"

# finding #1: a host with no working python3 must degrade the --check probe
# to a warning, never hard-fail the allow itself — this same script is what
# the daemon's operator-approve path (EgressBroker._apply_allow) shells out
# to, so a broken interpreter used to fail EVERY approval. PYTHON3 points at
# a nonexistent binary so require_python3 (src/common.sh) fails without ever
# reaching the --check invocation.
run_ae_no_python() { ( cd "$SBOX" && env BOTTLES_PATH="$WORK/no-such-manifests" \
    MOCK_EXISTING_CONTAINERS="${MOCK_EXISTING_CONTAINERS-djinn-mycontainer}" \
    PYTHON3=/nonexistent/python3 \
    PATH="$WORK/aebin:$PATH" bash bin/allow-egress.sh "$@" ) 2>&1; }
out=$(run_ae_no_python mycontainer skipped-check.example.com --save none); rc=$?
assert_rc "no working python3 → allow still proceeds (rc 0)" 0 "$rc"
assert_contains "no working python3 → deny-list check skipped, not failed" "$out" \
    "⚠ deny-list check skipped: no working python3"
assert_absent "no working python3 → --check is never invoked" "$out" \
    "deny-list check failed"

# ────────────────────────────────────────────────────────────────────────────
echo "── update-agent-keys.sh ──"
DAH="$WORK/dah"; KP="$DAH/keys/mysite"; mkdir -p "$KP"
# up.sh always composes one <agent>.env per enabled shim agent before this
# helper can run; the helper derives its valid-agent list from those files,
# so the fixture must seed them like a real keys dir.
for a in $SHIM; do : > "$KP/$a.env"; chmod 600 "$KP/$a.env"; done
uak() { ( cd "$SBOX" && env DJINN_HOME="$DAH" bash bin/update-agent-keys.sh "$@" ) ; }

uak mysite claude OBSIDIAN_ANNOTATED_KEY sekret >/dev/null
assert_eq "set writes VAR to <agent>.env" "OBSIDIAN_ANNOTATED_KEY=sekret" "$(cat "$KP/claude.env")"
assert_eq "edited file is mode 600" "600" "$(mode_of "$KP/claude.env")"
uak mysite claude OBSIDIAN_ANNOTATED_KEY newval >/dev/null
assert_eq "idempotent replace (one line, new value)" "OBSIDIAN_ANNOTATED_KEY=newval" "$(cat "$KP/claude.env")"
printf '\n' | uak mysite claude OBSIDIAN_ANNOTATED_KEY >/dev/null   # empty value → remove
assert_eq "empty value removes the var" "" "$(cat "$KP/claude.env")"
uak mysite common MCP_GATEWAY_TOKEN shared >/dev/null
allhave=1; for a in $SHIM; do grep -q '^MCP_GATEWAY_TOKEN=shared$' "$KP/$a.env" || allhave=0; done
assert_eq "common fans out to every shim agent" "1" "$allhave"
out=$(uak mysite 2>&1); rc=$?
assert_rc "list mode rc 0" 0 "$rc"
assert_contains "list mode shows var names" "$out" "MCP_GATEWAY_TOKEN"
out=$(uak nosuchcontainer claude VAR val 2>&1); rc=$?
assert_rc "missing keys dir rc 1" 1 "$rc"
out=$(uak mysite bogusagent VAR val 2>&1); rc=$?
assert_rc "unknown agent rc 1" 1 "$rc"
assert_contains "unknown agent message" "$out" "agent must be one of"

# Regression: the removal path of set_var_in used to end in
# `[ -n "$VALUE" ] && warn_unbound_org_token ...` — on a removal that `&&`
# returned 1 and, under the script's `set -e`, aborted after the first
# agent's file, leaving the var set in every file but the first. The
# warn/note helpers are gone now (per-owner routing is gone), but the
# removal path must still complete across every agent file.
RKP="$DAH/keys/removal"; mkdir -p "$RKP"
for a in one two three; do printf 'FOO=bar\n' > "$RKP/$a.env"; chmod 600 "$RKP/$a.env"; done
out=$(printf '\n' | uak removal common FOO 2>&1); rc=$?
assert_rc "common removal across every agent file exits 0" 0 "$rc"
allclear=1; for a in one two three; do grep -q '^FOO=' "$RKP/$a.env" && allclear=0; done
assert_eq "common removal clears the var from every agent file" "1" "$allclear"

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
echo "ran withsvc args=[$*] base=${BASE_PATH:+set} bottles=${BOTTLES_PATH:+set}"
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
# A launcher that reads a bottle (browser) must see the SAME
# BOTTLES_PATH up.sh does; unexported it would silently ignore ./.env.
assert_contains "dispatcher exports BOTTLES_PATH to the launcher" "$out" "bottles=set"

# ────────────────────────────────────────────────────────────────────────────
echo "── ./djinn allow routing ──"
DJBOX="$WORK/djbox"
mkdir -p "$DJBOX/bin" "$DJBOX/src"
cp "$REPO/djinn" "$DJBOX/"
cat > "$DJBOX/bin/allow-egress.sh" <<'MOCK'
#!/bin/bash
echo "ROUTE_ALLOW_EGRESS $*"
MOCK
chmod +x "$DJBOX/bin/allow-egress.sh"
cat > "$DJBOX/src/egress_watch.py" <<'MOCK'
#!/usr/bin/env python3
import sys
print("ROUTE_EGRESS_WATCH", " ".join(sys.argv[1:]))
MOCK
chmod +x "$DJBOX/src/egress_watch.py"
out=$(cd "$DJBOX" && ./djinn allow --watch extra 2>&1); rc=$?
assert_rc "allow --watch routes to egress_watch.py" 0 "$rc"
assert_contains "allow --watch execs watcher" "$out" "ROUTE_EGRESS_WATCH extra"
out=$(cd "$DJBOX" && ./djinn allow foo example.com 2>&1); rc=$?
assert_rc "allow <container> <domain> routes to allow-egress.sh" 0 "$rc"
assert_contains "allow forwards container and domain" "$out" "ROUTE_ALLOW_EGRESS foo example.com"

echo ""
if [ "$FAILURES" -gt 0 ]; then echo "FAILED: $FAILURES bash test(s)"; exit 1; fi
echo "all bash tests passed"
