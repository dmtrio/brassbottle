#!/usr/bin/env python3
"""Unit tests for src/remote_access.py — the RFC 04 remote-access decisions
extracted out of src/entrypoint.sh's START_SSHD branching and
src/init-firewall.sh's jump-scoped :22 rule (AGENTS.md "Python over bash").

No subprocess mocking needed (stdlib-only, no docker/network calls): the
`sshd` and `firewall-ssh` commands are pure decisions over env + (for sshd)
a key file. Every branch is exercised by calling cmd_sshd/cmd_firewall_ssh
directly with an explicit env dict and capturing sys.stdout/sys.stderr,
mirroring tests/test_jump_host.py's IpTests style. A handful of
subprocess-based tests at the bottom prove the real CLI's stdout evals
cleanly under bash, the same way tests/test_manifest.py's
test_render_shell_quoting_round_trips proves manifest.py's render().
"""

from __future__ import annotations

import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
import remote_access as ra  # noqa: E402

MODULE = Path(__file__).parent.parent / "src" / "remote_access.py"

OP_KEY = "ssh-ed25519 AAAAOP operator@mac"
JUMP_KEY = "ssh-ed25519 AAAAJUMP djinn-jump"


def _sshd_args(authorized_keys=None):
    argv = ["sshd"]
    if authorized_keys is not None:
        argv += ["--authorized-keys", str(authorized_keys)]
    return ra.build_parser().parse_args(argv)


def _run_sshd(env, authorized_keys=None):
    out, err = io.StringIO(), io.StringIO()
    with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
        rc = ra.cmd_sshd(_sshd_args(authorized_keys), env=env)
    return rc, out.getvalue(), err.getvalue()


def _run_firewall_ssh(env):
    out, err = io.StringIO(), io.StringIO()
    args = ra.build_parser().parse_args(["firewall-ssh"])
    with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
        rc = ra.cmd_firewall_ssh(args, env=env)
    return rc, out.getvalue(), err.getvalue()


class DecideSshdTests(unittest.TestCase):
    def test_published_with_key(self):
        self.assertEqual(
            ra.decide_sshd({"SSH_ENABLED": "true", "SSH_AUTHORIZED_KEY": OP_KEY}),
            ("published", True, "keys=operator"),
        )

    def test_published_without_key_is_fatal(self):
        self.assertEqual(
            ra.decide_sshd({"SSH_ENABLED": "true"}),
            ("published", False, "fatal"),
        )

    def test_published_with_both_keys_reports_both(self):
        # _rebuild_authorized_keys writes both keys whenever present,
        # regardless of mode — the reason string must reflect that, not
        # undercount to "keys=operator" just because this is the published
        # path.
        self.assertEqual(
            ra.decide_sshd(
                {"SSH_ENABLED": "true", "SSH_AUTHORIZED_KEY": OP_KEY, "JUMP_AUTHORIZED_KEY": JUMP_KEY}
            ),
            ("published", True, "keys=operator+jump"),
        )

    def test_published_ignores_enable_firewall(self):
        # The published path is unaffected by ENABLE_FIREWALL — it was
        # always meant to be reachable from the whole bridge (and the host),
        # not scoped to a firewall-enforced jump rule.
        self.assertEqual(
            ra.decide_sshd(
                {"SSH_ENABLED": "true", "SSH_AUTHORIZED_KEY": OP_KEY, "ENABLE_FIREWALL": "false"}
            ),
            ("published", True, "keys=operator"),
        )

    def test_jump_with_no_firewall_does_not_start(self):
        # ENABLE_FIREWALL=false means init-firewall.sh never runs, so there
        # is no jump-scoped :22 rule to enforce — starting sshd here would
        # leave it reachable from every sibling on the bridge, not just the
        # jump.
        self.assertEqual(
            ra.decide_sshd(
                {
                    "SSH_AUTHORIZED_KEY": OP_KEY,
                    "JUMP_AUTHORIZED_KEY": JUMP_KEY,
                    "ENABLE_FIREWALL": "false",
                }
            ),
            ("jump", False, "reason=no-firewall"),
        )

    def test_jump_no_firewall_wins_over_no_keys(self):
        # no-firewall is checked before the keys check — even with no keys
        # at all, the reported reason should be the firewall one (it's the
        # more actionable diagnosis: keys wouldn't help either way).
        self.assertEqual(
            ra.decide_sshd({"ENABLE_FIREWALL": "false"}),
            ("jump", False, "reason=no-firewall"),
        )

    def test_jump_default_with_both_keys(self):
        self.assertEqual(
            ra.decide_sshd({"SSH_AUTHORIZED_KEY": OP_KEY, "JUMP_AUTHORIZED_KEY": JUMP_KEY}),
            ("jump", True, "keys=operator+jump"),
        )

    def test_jump_with_jump_key_only(self):
        self.assertEqual(
            ra.decide_sshd({"JUMP_AUTHORIZED_KEY": JUMP_KEY}),
            ("jump", True, "keys=jump"),
        )

    def test_jump_with_operator_key_only(self):
        self.assertEqual(
            ra.decide_sshd({"SSH_AUTHORIZED_KEY": OP_KEY}),
            ("jump", True, "keys=operator"),
        )

    def test_jump_with_no_keys(self):
        self.assertEqual(ra.decide_sshd({}), ("jump", False, "reason=no-keys"))

    def test_remote_jump_false_with_nothing_is_off(self):
        self.assertEqual(
            ra.decide_sshd({"REMOTE_JUMP": "false"}),
            ("off", False, "reason=remote-jump-false"),
        )

    def test_remote_jump_false_still_off_even_with_keys(self):
        # remote.jump: false is an explicit opt-out of the DEFAULT path —
        # only ssh: true (SSH_ENABLED) can start sshd once REMOTE_JUMP=false.
        self.assertEqual(
            ra.decide_sshd({"REMOTE_JUMP": "false", "SSH_AUTHORIZED_KEY": OP_KEY}),
            ("off", False, "reason=remote-jump-false"),
        )

    def test_published_wins_over_remote_jump_false(self):
        self.assertEqual(
            ra.decide_sshd(
                {"SSH_ENABLED": "true", "SSH_AUTHORIZED_KEY": OP_KEY, "REMOTE_JUMP": "false"}
            ),
            ("published", True, "keys=operator"),
        )


class CmdSshdTests(unittest.TestCase):
    def test_published_with_key_prints_evalable_assignments(self):
        rc, out, err = _run_sshd({"SSH_ENABLED": "true", "SSH_AUTHORIZED_KEY": OP_KEY})
        self.assertEqual(rc, 0)
        self.assertEqual(out, "SSHD_MODE=published\nSTART_SSHD=true\n")
        self.assertIn("remote_access sshd mode=published start=true keys=operator", err)

    def test_published_without_key_exits_1_with_fatal_lines(self):
        rc, out, err = _run_sshd({"SSH_ENABLED": "true"})
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")  # nothing to eval — caller's set -e must abort
        self.assertIn("FATAL: SSH_ENABLED=true but SSH_AUTHORIZED_KEY is empty.", err)
        self.assertIn("Set SSH_AUTHORIZED_KEY in ~/djinn/secrets.env", err)

    def test_jump_no_keys_prints_the_reachability_warning_verbatim(self):
        rc, out, err = _run_sshd({})
        self.assertEqual(rc, 0)
        self.assertEqual(out, "SSHD_MODE=jump\nSTART_SSHD=false\n")
        self.assertIn(
            "⚠ Jump reachability: no SSH keys (JUMP_AUTHORIZED_KEY unset — "
            "run ./djinn jump start and add it to secrets.env); sshd not started",
            err,
        )
        self.assertIn("remote_access sshd mode=jump start=false reason=no-keys", err)

    def test_jump_no_firewall_prints_the_reachability_warning_verbatim(self):
        rc, out, err = _run_sshd(
            {"SSH_AUTHORIZED_KEY": OP_KEY, "JUMP_AUTHORIZED_KEY": JUMP_KEY, "ENABLE_FIREWALL": "false"}
        )
        self.assertEqual(rc, 0)
        self.assertEqual(out, "SSHD_MODE=jump\nSTART_SSHD=false\n")
        self.assertIn(
            "⚠ Jump reachability: ENABLE_FIREWALL=false — the jump-only :22 "
            "rule cannot be enforced; sshd not started (set ssh: to publish "
            "explicitly)",
            err,
        )
        self.assertIn("remote_access sshd mode=jump start=false reason=no-firewall", err)

    def test_published_no_firewall_still_starts(self):
        rc, out, err = _run_sshd(
            {"SSH_ENABLED": "true", "SSH_AUTHORIZED_KEY": OP_KEY, "ENABLE_FIREWALL": "false"}
        )
        self.assertEqual(rc, 0)
        self.assertEqual(out, "SSHD_MODE=published\nSTART_SSHD=true\n")
        self.assertNotIn("⚠", err)

    def test_off_mode_prints_no_reachability_warning(self):
        # The ⚠ no-keys warning is jump-specific; remote.jump: false is a
        # deliberate opt-out and must not look like a degraded jump path.
        rc, out, err = _run_sshd({"REMOTE_JUMP": "false"})
        self.assertEqual(rc, 0)
        self.assertEqual(out, "SSHD_MODE=off\nSTART_SSHD=false\n")
        self.assertNotIn("⚠", err)


class AuthorizedKeysTests(unittest.TestCase):
    def test_published_writes_operator_key_only_mode_0600(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "authorized_keys"
            rc, _, err = _run_sshd(
                {"SSH_ENABLED": "true", "SSH_AUTHORIZED_KEY": OP_KEY}, authorized_keys=path
            )
            self.assertEqual(rc, 0)
            self.assertEqual(path.read_text(), f"{OP_KEY}\n")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertIn("remote_access authorized_keys path=", err)
            self.assertIn("action=write count=1", err)

    def test_jump_both_keys_operator_then_jump_trailing_newline(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "authorized_keys"
            rc, _, err = _run_sshd(
                {"SSH_AUTHORIZED_KEY": OP_KEY, "JUMP_AUTHORIZED_KEY": JUMP_KEY},
                authorized_keys=path,
            )
            self.assertEqual(rc, 0)
            self.assertEqual(path.read_text(), f"{OP_KEY}\n{JUMP_KEY}\n")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertIn("action=write count=2", err)

    def test_jump_key_only(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "authorized_keys"
            _run_sshd({"JUMP_AUTHORIZED_KEY": JUMP_KEY}, authorized_keys=path)
            self.assertEqual(path.read_text(), f"{JUMP_KEY}\n")

    def test_operator_key_only(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "authorized_keys"
            _run_sshd({"SSH_AUTHORIZED_KEY": OP_KEY}, authorized_keys=path)
            self.assertEqual(path.read_text(), f"{OP_KEY}\n")

    def test_jump_no_firewall_leaves_no_file_behind(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "authorized_keys"
            rc, _, err = _run_sshd(
                {
                    "SSH_AUTHORIZED_KEY": OP_KEY,
                    "JUMP_AUTHORIZED_KEY": JUMP_KEY,
                    "ENABLE_FIREWALL": "false",
                },
                authorized_keys=path,
            )
            self.assertEqual(rc, 0)
            self.assertFalse(path.exists())
            self.assertIn("action=remove count=0", err)

    def test_published_no_firewall_writes_the_key_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "authorized_keys"
            rc, _, _ = _run_sshd(
                {"SSH_ENABLED": "true", "SSH_AUTHORIZED_KEY": OP_KEY, "ENABLE_FIREWALL": "false"},
                authorized_keys=path,
            )
            self.assertEqual(rc, 0)
            self.assertEqual(path.read_text(), f"{OP_KEY}\n")

    def test_no_keys_leaves_no_file_behind(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "authorized_keys"
            rc, _, err = _run_sshd({}, authorized_keys=path)
            self.assertEqual(rc, 0)
            self.assertFalse(path.exists())
            self.assertIn("action=remove count=0", err)

    def test_stale_file_is_removed_when_not_starting(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "authorized_keys"
            path.write_text("stale-key-from-a-previous-boot\n")
            rc, _, _ = _run_sshd({"REMOTE_JUMP": "false"}, authorized_keys=path)
            self.assertEqual(rc, 0)
            self.assertFalse(path.exists())

    def test_a_dropped_key_is_not_left_over_from_a_stale_file(self):
        # Truncate/rebuild, not append: a key dropped from secrets.env must
        # actually stop working, not survive from a previous boot.
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "authorized_keys"
            path.write_text(f"{OP_KEY}\nstale-extra-key\n")
            _run_sshd({"SSH_AUTHORIZED_KEY": OP_KEY}, authorized_keys=path)
            self.assertEqual(path.read_text(), f"{OP_KEY}\n")

    def test_fatal_path_never_touches_the_key_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "authorized_keys"
            path.write_text("pre-existing\n")
            rc, _, _ = _run_sshd({"SSH_ENABLED": "true"}, authorized_keys=path)
            self.assertEqual(rc, 1)
            self.assertEqual(path.read_text(), "pre-existing\n")


class DecideFirewallSshTests(unittest.TestCase):
    def test_published_path_is_open(self):
        self.assertEqual(
            ra.decide_firewall_ssh({"SSH_ENABLED": "true"}),
            ("open", "", "published path", False),
        )

    def test_jump_with_valid_ip(self):
        self.assertEqual(
            ra.decide_firewall_ssh({"DJINN_JUMP_IP": "172.30.0.254"}),
            ("jump", "172.30.0.254", "jump 172.30.0.254", False),
        )

    def test_jump_with_empty_ip_is_none_not_error(self):
        rule, ip, reason, invalid = ra.decide_firewall_ssh({})
        self.assertEqual((rule, ip, invalid), ("none", "", False))
        self.assertIn("DJINN_JUMP_IP empty", reason)

    def test_jump_with_invalid_ip_is_an_error(self):
        rule, ip, reason, invalid = ra.decide_firewall_ssh({"DJINN_JUMP_IP": "not-an-ip"})
        self.assertTrue(invalid)

    def test_opt_out(self):
        self.assertEqual(
            ra.decide_firewall_ssh({"REMOTE_JUMP": "false"}),
            ("none", "", "opt-out", False),
        )

    def test_ipv4_address_validation_rejects_a_hostname(self):
        # Validate with ipaddress.IPv4Address, not a regex — a hostname must
        # not slip through as if it were a source IP for iptables -s.
        _, _, _, invalid = ra.decide_firewall_ssh({"DJINN_JUMP_IP": "jump.example.com"})
        self.assertTrue(invalid)

    def test_ipv6_is_also_rejected(self):
        _, _, _, invalid = ra.decide_firewall_ssh({"DJINN_JUMP_IP": "::1"})
        self.assertTrue(invalid)


class CmdFirewallSshTests(unittest.TestCase):
    def test_open_prints_evalable_assignments(self):
        rc, out, err = _run_firewall_ssh({"SSH_ENABLED": "true"})
        self.assertEqual(rc, 0)
        self.assertEqual(out, "SSH_INPUT_RULE=open\nDJINN_JUMP_IP=''\n")
        self.assertIn("remote_access firewall-ssh rule=open reason=published path", err)

    def test_jump_prints_the_validated_ip_back_as_djinn_jump_ip(self):
        # tests/remote.test.sh pins the LITERAL `-s "$DJINN_JUMP_IP"` in
        # init-firewall.sh — the module must re-emit under that same name,
        # not a renamed SSH_INPUT_SOURCE, so that literal keeps working.
        rc, out, err = _run_firewall_ssh({"DJINN_JUMP_IP": "172.30.0.254"})
        self.assertEqual(rc, 0)
        self.assertEqual(out, "SSH_INPUT_RULE=jump\nDJINN_JUMP_IP=172.30.0.254\n")

    def test_none_when_ip_empty(self):
        rc, out, err = _run_firewall_ssh({})
        self.assertEqual(rc, 0)
        self.assertEqual(out, "SSH_INPUT_RULE=none\nDJINN_JUMP_IP=''\n")
        self.assertIn("DJINN_JUMP_IP empty", err)

    def test_invalid_ip_exits_1_with_no_stdout(self):
        rc, out, err = _run_firewall_ssh({"DJINN_JUMP_IP": "999.999.999.999"})
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")  # nothing to eval — caller's set -e must abort
        self.assertIn("ERROR: Invalid DJINN_JUMP_IP: 999.999.999.999", err)

    def test_opt_out_prints_none(self):
        rc, out, err = _run_firewall_ssh({"REMOTE_JUMP": "false"})
        self.assertEqual(rc, 0)
        self.assertEqual(out, "SSH_INPUT_RULE=none\nDJINN_JUMP_IP=''\n")
        self.assertIn("reason=opt-out", err)


class ParserTests(unittest.TestCase):
    def test_every_subcommand_parses(self):
        parser = ra.build_parser()
        for argv in (["sshd"], ["sshd", "--authorized-keys", "/tmp/x"], ["firewall-ssh"]):
            with self.subTest(argv=argv):
                parser.parse_args(argv)

    def test_missing_subcommand_exits(self):
        with self.assertRaises(SystemExit):
            ra.build_parser().parse_args([])


class SubprocessEvalTests(unittest.TestCase):
    """Proves the real CLI's stdout evals cleanly under bash — not just that
    the print() calls look right in isolation. Same shape as
    tests/test_manifest.py's test_render_shell_quoting_round_trips."""

    def _eval_in_bash(self, argv, env, echo_vars):
        result = subprocess.run(
            [sys.executable, str(MODULE), *argv],
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        echo_cmd = "; ".join(f'echo "${v}"' for v in echo_vars)
        bash = subprocess.run(
            ["bash", "-c", f'eval "$(cat)"; {echo_cmd}'],
            input=result.stdout,
            capture_output=True,
            text=True,
        )
        self.assertEqual(bash.returncode, 0, bash.stderr)
        return bash.stdout.splitlines()

    def test_sshd_stdout_evals_cleanly(self):
        env = {"PATH": "/usr/bin:/bin", "SSH_ENABLED": "true", "SSH_AUTHORIZED_KEY": OP_KEY}
        lines = self._eval_in_bash(["sshd"], env, ["SSHD_MODE", "START_SSHD"])
        self.assertEqual(lines, ["published", "true"])

    def test_firewall_ssh_stdout_evals_cleanly(self):
        env = {"PATH": "/usr/bin:/bin", "DJINN_JUMP_IP": "172.30.0.254"}
        lines = self._eval_in_bash(
            ["firewall-ssh"], env, ["SSH_INPUT_RULE", "DJINN_JUMP_IP"]
        )
        self.assertEqual(lines, ["jump", "172.30.0.254"])

    def test_published_no_key_exit_1_and_prints_fatal_to_stderr(self):
        result = subprocess.run(
            [sys.executable, str(MODULE), "sshd"],
            env={"PATH": "/usr/bin:/bin", "SSH_ENABLED": "true"},
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("FATAL: SSH_ENABLED=true but SSH_AUTHORIZED_KEY is empty.", result.stderr)

    def test_py_compiles(self):
        result = subprocess.run(
            [sys.executable, "-m", "py_compile", str(MODULE)], capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
