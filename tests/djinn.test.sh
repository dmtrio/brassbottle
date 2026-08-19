#!/bin/bash
# tests/djinn.test.sh — host-runnable checks for the ./djinn dispatcher.
# djinn is pure glue (case → exec), so this suite only proves the wiring:
# usage/help text, exit codes for the no-args/help/unknown-subcommand paths,
# that every subcommand's target file exists (and is executable, where that
# applies), and that each subcommand really execs its target — compared
# byte-for-byte against calling the target directly on the arg-free
# invocation every target already knows how to refuse without docker.

# SC2015 (`A && pass || fail` is not if-else): intentional — pass() is a bare
# echo and cannot fail, so the || arm only runs when the check fails.
# shellcheck disable=SC2015

# set -u only (not -e): most checks below deliberately capture a nonzero exit
# code via `out=$(...); rc=$?` — under set -e that assignment would abort the
# whole suite the first time a subcommand is EXPECTED to fail (same reasoning
# as tests/bash.test.sh).
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "$SCRIPT_DIR"

FAILURES=0
fail() { echo "  ✗ $1"; FAILURES=$((FAILURES + 1)); }
pass() { echo "  ✓ $1"; }
assert_rc() { if [ "$2" = "$3" ]; then pass "$1"; else fail "$1 (expected rc $2, got $3)"; fi; }
assert_contains() { case "$2" in *"$3"*) pass "$1" ;; *) fail "$1"; printf '     missing [%s] in: [%s]\n' "$3" "$2" ;; esac; }

echo "── ./djinn: no args ──"
out=$(./djinn 2>&1); rc=$?
assert_rc "no args → usage rc 1" 1 "$rc"
assert_contains "no args → usage text" "$out" "Usage: ./djinn"

echo "── ./djinn: help / -h / --help ──"
for h in help -h --help; do
    out=$(./djinn "$h" 2>&1); rc=$?
    assert_rc "'$h' → rc 0" 0 "$rc"
    for sub in up down service allow keys; do
        assert_contains "'$h' mentions '$sub'" "$out" "  $sub "
    done
done

echo "── ./djinn: unknown subcommand ──"
out=$(./djinn bogus 2>&1); rc=$?
assert_rc "unknown subcommand → rc 1" 1 "$rc"
assert_contains "unknown subcommand names it" "$out" "unknown subcommand 'bogus'"

echo "── ./djinn: every subcommand target exists (and is executable, where that applies) ──"
check_target() {   # $1=subcommand $2=target-path $3=1 if it must be +x
    if [ -f "$2" ]; then pass "$1 → $2 exists"; else fail "$1 → $2 missing"; fi
    if [ "$3" = 1 ]; then
        [ -x "$2" ] && pass "$1 → $2 is executable" || fail "$1 → $2 is not executable"
    else
        [ -r "$2" ] && pass "$1 → $2 is readable (invoked via python3, not exec'd directly)" \
            || fail "$1 → $2 is not readable"
    fi
}
check_target up up.sh 1
check_target down down.sh 1
check_target service service.sh 1
check_target allow bin/allow-egress.sh 1
check_target keys bin/update-agent-keys.sh 1

echo "── ./djinn is itself executable ──"
[ -x ./djinn ] && pass "./djinn is executable" || fail "./djinn is not executable"

echo "── ./djinn: dispatch reaches the right target (arg-free refusal paths, no docker) ──"
# Every target below refuses a bare/no-arg invocation with a usage message and
# a nonzero exit BEFORE touching docker (verified: none of these leave a
# stray .djinn dir or call out to docker) — safe to run for real
# here, and an exact byte-for-byte match against the direct call is the
# strongest proof djinn's case→exec forwarded to the right script with the
# right args (none).
compare() {   # $1=label $2=direct-cmd(as string) $3=via-djinn-subcommand
    d=$(eval "$2" 2>&1; echo "rc=$?")
    v=$(./djinn "$3" 2>&1; echo "rc=$?")
    [ "$d" = "$v" ] && pass "djinn $1 forwards to $2" || {
        fail "djinn $1 output differs from $2"
        printf '     direct: [%s]\n     via djinn: [%s]\n' "$d" "$v"
    }
}
compare "up" "./up.sh" up
compare "down" "./down.sh" down
compare "service" "./service.sh" service
compare "allow" "./bin/allow-egress.sh" allow
compare "keys" "./bin/update-agent-keys.sh" keys

echo ""
if [ "$FAILURES" -gt 0 ]; then echo "FAILED: $FAILURES djinn test(s)"; exit 1; fi
echo "all djinn tests passed"
