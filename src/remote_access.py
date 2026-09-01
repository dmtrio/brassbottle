#!/usr/bin/env python3
"""remote_access.py — RFC 04 remote-access decisions for the entrypoint and
the firewall.

Repo rule "Python over bash" (AGENTS.md): src/entrypoint.sh's START_SSHD
decision (+ authorized_keys rebuild) and src/init-firewall.sh's jump-scoped
:22 rule outgrew linear bash — this module owns both branching decisions,
unit-tested here (tests/test_remote_access.py). The callers `eval` its
stdout; stdlib only.

Two subcommands, each prints shell-quoted `NAME=value` assignment lines to
stdout (shlex.quote) for the caller to `eval`, and logs one boundary line to
stderr saying what it decided and why:

    sshd [--authorized-keys PATH]
        Reads SSH_ENABLED, REMOTE_JUMP (default true), SSH_AUTHORIZED_KEY,
        JUMP_AUTHORIZED_KEY, ENABLE_FIREWALL (default true — jump path only;
        with no firewall there is no jump-scoped :22 rule to enforce, so
        sshd is refused rather than left reachable from the whole bridge).
        Emits SSHD_MODE=published|jump|off and
        START_SSHD=true|false. The published-with-no-key case prints the two
        FATAL lines to stderr and exits 1 — under `eval "$(...)"` + set -e,
        that aborts the entrypoint exactly as it did before this refactor.
        With --authorized-keys, also rebuilds that file (truncate; operator
        key then jump key, only the ones present; mode 0600) when
        START_SSHD, or removes a stale one when not — the same file, one
        write path, instead of the entrypoint's own truncate/append/rm-f.

    firewall-ssh
        Reads SSH_ENABLED, REMOTE_JUMP, DJINN_JUMP_IP. Emits
        SSH_INPUT_RULE=open|jump|none and DJINN_JUMP_IP=<validated address or
        empty> — re-emitted (not renamed) so init-firewall.sh's existing
        `-s "$DJINN_JUMP_IP"` literal (pinned by tests/remote.test.sh) keeps
        working unchanged. An invalid non-empty DJINN_JUMP_IP prints an ERROR
        line to stderr and exits 1 (fail-closed under the firewall's
        `set -euo pipefail`). Validated with ipaddress.IPv4Address, not a
        regex.
"""

from __future__ import annotations

import argparse
import ipaddress
import os
import shlex
import sys
from pathlib import Path
from typing import Mapping, Sequence, Tuple


def _flag(env: Mapping[str, str], name: str, default: bool) -> bool:
    """A djinn boolean env var: unset falls back to `default`; set compares
    literally against 'true' — the same convention every bash caller of this
    module already used (`[ "$X" = "true" ]`)."""
    raw = env.get(name)
    if raw is None:
        return default
    return raw == "true"


def decide_sshd(env: Mapping[str, str]) -> Tuple[str, bool, str]:
    """Returns (mode, start, reason).

    mode is 'published' (explicit ssh:), 'jump' (default path) or 'off'
    (remote.jump: false and no ssh:). reason is 'fatal' for the
    published-with-no-key case (caller prints the FATAL lines and exits 1);
    otherwise a short token for the boundary log (keys=..., reason=...).
    """
    ssh_enabled = _flag(env, "SSH_ENABLED", False)
    remote_jump = _flag(env, "REMOTE_JUMP", True)
    ssh_key = env.get("SSH_AUTHORIZED_KEY", "")
    jump_key = env.get("JUMP_AUTHORIZED_KEY", "")
    # Built once, used by both modes' reason string: _rebuild_authorized_keys
    # writes BOTH keys whenever they're present, regardless of mode (the
    # published path doesn't strip a JUMP_AUTHORIZED_KEY that happens to also
    # be set) — so the boundary log must say "keys=operator+jump" there too,
    # not just "keys=operator", or it undercounts what actually landed in
    # authorized_keys.
    have = []
    if ssh_key:
        have.append("operator")
    if jump_key:
        have.append("jump")

    if ssh_enabled:
        # Explicit ssh: (host-published) — fail loud rather than start sshd
        # nobody can log into.
        if not ssh_key:
            return "published", False, "fatal"
        return "published", True, "keys=" + "+".join(have)

    if remote_jump:
        # Default path: every bottle is jump-reachable unless remote.jump:
        # false. Either key alone is enough (operator key or the jump's
        # own); with neither, sshd simply doesn't start — the firewall rule
        # is equally harmless in that case, since nothing is listening.
        #
        # ENABLE_FIREWALL=false means init-firewall.sh never runs at all, so
        # there is no jump-scoped :22 rule to enforce — every sibling on the
        # shared bridge could reach sshd, not just the jump. Refuse to start
        # it rather than silently widen who can reach this bottle; the
        # explicit ssh: (published) path is unaffected, since that path was
        # always meant to be reachable from the whole bridge anyway.
        if not _flag(env, "ENABLE_FIREWALL", True):
            return "jump", False, "reason=no-firewall"
        if have:
            return "jump", True, "keys=" + "+".join(have)
        return "jump", False, "reason=no-keys"

    return "off", False, "reason=remote-jump-false"


def _rebuild_authorized_keys(path: Path, ssh_key: str, jump_key: str, start: bool) -> int:
    """Truncate/rebuild `path` (operator key then jump key, only the ones
    present) when `start`; remove a stale file otherwise.

    Order matters: the operator key must keep working even when the jump is
    down, so it always leads; the jump key is appended, never replacing.
    Returns the key count written (0 on removal) for the caller's boundary
    log.
    """
    if start:
        keys = [k for k in (ssh_key, jump_key) if k]
        path.write_text("".join(f"{k}\n" for k in keys), encoding="utf-8")
        path.chmod(0o600)
        return len(keys)
    path.unlink(missing_ok=True)
    return 0


def cmd_sshd(args: argparse.Namespace, env: Mapping[str, str] | None = None) -> int:
    env = env if env is not None else os.environ
    mode, start, reason = decide_sshd(env)

    if reason == "fatal":
        print("FATAL: SSH_ENABLED=true but SSH_AUTHORIZED_KEY is empty.", file=sys.stderr)
        print("Set SSH_AUTHORIZED_KEY in ~/djinn/secrets.env (your public key).", file=sys.stderr)
        return 1

    if args.authorized_keys:
        path = Path(args.authorized_keys)
        count = _rebuild_authorized_keys(
            path, env.get("SSH_AUTHORIZED_KEY", ""), env.get("JUMP_AUTHORIZED_KEY", ""), start
        )
        print(
            f"remote_access authorized_keys path={path} "
            f"action={'write' if start else 'remove'} count={count}",
            file=sys.stderr,
        )

    if mode == "jump" and not start and reason == "reason=no-firewall":
        print(
            "⚠ Jump reachability: ENABLE_FIREWALL=false — the jump-only :22 "
            "rule cannot be enforced; sshd not started (set ssh: to publish "
            "explicitly)",
            file=sys.stderr,
        )
    elif mode == "jump" and not start:
        print(
            "⚠ Jump reachability: no SSH keys (JUMP_AUTHORIZED_KEY unset — "
            "run ./djinn jump start and add it to secrets.env); sshd not started",
            file=sys.stderr,
        )

    print(
        f"remote_access sshd mode={mode} start={'true' if start else 'false'} {reason}",
        file=sys.stderr,
    )
    print(f"SSHD_MODE={shlex.quote(mode)}")
    print(f"START_SSHD={shlex.quote('true' if start else 'false')}")
    return 0


def decide_firewall_ssh(env: Mapping[str, str]) -> Tuple[str, str, str, bool]:
    """Returns (rule, jump_ip, reason, invalid).

    rule is 'open' (explicit ssh:), 'jump' (scoped to jump_ip) or 'none'.
    invalid True means a non-empty DJINN_JUMP_IP failed IPv4 validation —
    the caller prints an ERROR line and exits 1 rather than trusting it into
    an iptables -s argument.
    """
    ssh_enabled = _flag(env, "SSH_ENABLED", False)
    remote_jump = _flag(env, "REMOTE_JUMP", True)
    raw_ip = env.get("DJINN_JUMP_IP", "")

    if ssh_enabled:
        # Never both: an explicit ssh: bottle already gets the wide-open
        # published rule, so a jump-scoped rule on top would only suggest a
        # narrower path exists when it doesn't.
        return "open", "", "published path", False

    if remote_jump:
        if not raw_ip:
            # Non-fatal, matches the entrypoint's own no-keys degradation:
            # sshd (if it started at all) simply has no ACCEPT rule yet.
            return (
                "none",
                "",
                "REMOTE_JUMP=true but DJINN_JUMP_IP empty — sshd (if "
                "running) unreachable until the jump address is known",
                False,
            )
        try:
            ipaddress.IPv4Address(raw_ip)
        except ValueError:
            return "none", raw_ip, "invalid", True
        return "jump", raw_ip, f"jump {raw_ip}", False

    return "none", "", "opt-out", False


def cmd_firewall_ssh(args: argparse.Namespace, env: Mapping[str, str] | None = None) -> int:
    del args  # no flags of its own
    env = env if env is not None else os.environ
    rule, jump_ip, reason, invalid = decide_firewall_ssh(env)

    if invalid:
        print(f"ERROR: Invalid DJINN_JUMP_IP: {env.get('DJINN_JUMP_IP', '')}", file=sys.stderr)
        return 1

    print(f"remote_access firewall-ssh rule={rule} reason={reason}", file=sys.stderr)
    print(f"SSH_INPUT_RULE={shlex.quote(rule)}")
    print(f"DJINN_JUMP_IP={shlex.quote(jump_ip)}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="remote_access.py",
        description="RFC 04 remote-access decisions for entrypoint.sh and init-firewall.sh.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sshd = sub.add_parser("sshd", help="decide START_SSHD and rebuild authorized_keys")
    sshd.add_argument(
        "--authorized-keys", default=None, metavar="PATH",
        help="also rebuild (or remove) this authorized_keys file",
    )
    sub.add_parser("firewall-ssh", help="decide the inbound :22 iptables rule")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "sshd":
        return cmd_sshd(args)
    return cmd_firewall_ssh(args)


if __name__ == "__main__":
    sys.exit(main())
