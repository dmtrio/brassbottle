#!/usr/bin/env python3
"""Unit tests for B3 egress broker iptables rules and supervisor watchdog."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

import egress_broker as broker  # noqa: E402

FIREWALL_SCRIPT = REPO_ROOT / "src" / "egress_broker_firewall.sh"
INIT_FIREWALL = REPO_ROOT / "src" / "init-firewall.sh"
ENTRYPOINT = REPO_ROOT / "src" / "entrypoint.sh"


def _nat_rule_args(code: str, func_name: str) -> list[str]:
    """Tokenize the nat-table OUTPUT rule inside one shell function body.

    Line continuations are folded so the rule reads as one argument list,
    which is what iptables actually receives.
    """
    body = code.split(f"{func_name}() {{", 1)[1].split("\n}", 1)[0]
    folded = body.replace("\\\n", " ")
    for line in folded.splitlines():
        if "-t nat" in line:
            # Drop the remove path's `2>/dev/null || true` tail: it is shell,
            # not part of the argument list iptables sees.
            args = line.split()
            if "2>/dev/null" in args:
                args = args[: args.index("2>/dev/null")]
            return args
    raise AssertionError(f"no nat rule found in {func_name}")


class FirewallScriptTests(unittest.TestCase):
    def test_bash_syntax_clean(self):
        for path in (FIREWALL_SCRIPT, INIT_FIREWALL, ENTRYPOINT):
            with self.subTest(path=path.name):
                proc = subprocess.run(
                    ["bash", "-n", str(path)],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_dry_run_emits_nat_and_filter_rules(self):
        proc = subprocess.run(
            ["bash", str(FIREWALL_SCRIPT), "dry-run"],
            capture_output=True,
            text=True,
            check=True,
        )
        lines = proc.stdout.strip().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn("-t nat", lines[0])
        self.assertIn("REDIRECT", lines[0])
        self.assertIn("--dports 80,443", lines[0])
        self.assertIn("djinnbroker", lines[0])
        # Loopback is not egress: without the exclusion a local service on
        # :80/:443 is REDIRECTed into the broker and filed with the operator
        # as an un-allowable IP-literal request.
        self.assertIn("! -d 127.0.0.0/8", lines[0])
        self.assertIn("127.0.0.1", lines[1])
        self.assertIn("--dport 3128", lines[1])
        self.assertIn("-j ACCEPT", lines[1])

    def test_kill_switch_off_omits_broker_rules(self):
        text = INIT_FIREWALL.read_text(encoding="utf-8")
        marker = 'if [ "${ENABLE_EGRESS_BROKER:-true}" = "true" ]; then'
        start = text.index(marker)
        end = text.index("\nfi\n", start)
        enabled_block = text[start:end]
        outside = text[:start] + text[end + len("\nfi\n") :]
        self.assertIn("egress_broker_firewall.sh add", enabled_block)
        self.assertNotIn("egress_broker_firewall.sh add", outside)
        self.assertNotIn("--nflog-group 32", outside)

    def test_broker_accept_precedes_nflog_precedes_reject(self):
        text = INIT_FIREWALL.read_text(encoding="utf-8")
        marker = 'if [ "${ENABLE_EGRESS_BROKER:-true}" = "true" ]; then'
        start = text.index(marker)
        end = text.index("\nfi\n", start)
        enabled_block = text[start:end]
        broker_pos = enabled_block.index("egress_broker_firewall.sh add")
        nflog_pos = enabled_block.index("--nflog-group 32")
        reject_pos = text.index("REJECT --reject-with icmp-admin-prohibited")
        self.assertLess(broker_pos, nflog_pos, "broker ACCEPT must be installed before NFLOG")
        self.assertLess(nflog_pos, reject_pos, "NFLOG must precede the final REJECT")

    def test_remove_deletes_the_exact_rule_dry_run_adds(self):
        # An -A/-D mismatch leaves the REDIRECT installed after `remove`, so
        # the nat rule the script deletes must match the one it adds argument
        # for argument. Comments are stripped first: the exclusion is also
        # named in prose above the rule, which must not count as a rule.
        code = "\n".join(
            line
            for line in FIREWALL_SCRIPT.read_text(encoding="utf-8").splitlines()
            if not line.lstrip().startswith("#")
        )
        add = _nat_rule_args(code, "egress_broker_firewall_nat_rule")
        remove = _nat_rule_args(code, "egress_broker_firewall_remove")
        self.assertIn(["!", "-d", "127.0.0.0/8"], [add[i:i + 3] for i in range(len(add))])
        self.assertEqual(
            [a for a in add if a not in ("-A", "echo")],
            [a for a in remove if a not in ("-D", "iptables")],
        )

    def test_remove_invokes_iptables_delete(self):
        calls: list[list[str]] = []

        def runner(argv: list[str], **kwargs: object) -> mock.Mock:
            calls.append(argv)
            return mock.Mock(returncode=0)

        broker.remove_broker_firewall_rules(runner=runner)
        self.assertEqual(calls, [[str(FIREWALL_SCRIPT), "remove"]])


class SupervisorWatchdogTests(unittest.TestCase):
    def test_listen_failure_removes_firewall_rules(self):
        config = broker.BrokerConfig(
            container="coding-brassbottle",
            broker_url="http://127.0.0.1:8816",
            broker_token="secret",
            listen_port=broker.BROKER_LISTEN_PORT + 1,
        )
        removed = threading.Event()
        calls: list[list[str]] = []

        def runner(argv: list[str], **kwargs: object) -> mock.Mock:
            calls.append(argv)
            removed.set()
            return mock.Mock(returncode=0)

        with mock.patch.object(os, "geteuid", return_value=0), mock.patch.object(
            os, "fork", return_value=12345
        ), mock.patch.object(
            broker,
            "wait_for_broker_listen",
            return_value=False,
        ), mock.patch.object(os, "kill"), mock.patch.object(os, "waitpid"):
            rc = broker.supervise_broker(
                config,
                listen_timeout=0.1,
                firewall_runner=runner,
            )

        self.assertEqual(rc, 1)
        self.assertTrue(removed.is_set())
        self.assertEqual(calls, [[str(FIREWALL_SCRIPT), "remove"]])

    def test_broker_exit_removes_firewall_rules(self):
        config = broker.BrokerConfig(
            container="coding-brassbottle",
            broker_url="http://127.0.0.1:8816",
            broker_token="secret",
        )
        removed = threading.Event()
        calls: list[list[str]] = []

        def runner(argv: list[str], **kwargs: object) -> mock.Mock:
            calls.append(argv)
            removed.set()
            return mock.Mock(returncode=0)

        with mock.patch.object(os, "geteuid", return_value=0), mock.patch.object(
            os, "fork", return_value=999
        ), mock.patch.object(
            broker,
            "wait_for_broker_listen",
            return_value=True,
        ), mock.patch.object(os, "waitpid", return_value=(999, 0)):
            rc = broker.supervise_broker(config, firewall_runner=runner)

        self.assertEqual(rc, 1)
        self.assertTrue(removed.is_set())
        self.assertEqual(calls, [[str(FIREWALL_SCRIPT), "remove"]])


class RequestIdGenerationTests(unittest.TestCase):
    def test_generate_request_id_is_eight_hex_chars(self):
        request_id = broker.generate_request_id()
        self.assertEqual(len(request_id), 8)
        self.assertTrue(all(ch in "0123456789abcdef" for ch in request_id))


if __name__ == "__main__":
    unittest.main()
