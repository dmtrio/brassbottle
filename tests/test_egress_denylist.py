#!/usr/bin/env python3
"""Unit tests for src/egress_denylist.py (persistent egress deny list)."""

from __future__ import annotations

import contextlib
import io
import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(TESTS_DIR))
import egress_broker_host as broker_host  # noqa: E402
import egress_denylist as dl  # noqa: E402
from egress_test_sync import wait_for_tcp_listening  # noqa: E402

NOW = datetime(2026, 8, 27, 21, 40, 0, tzinfo=timezone.utc)


def _run_main(argv: list[str]) -> tuple[int, str, str]:
    """Run dl.main(argv), capturing (rc, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = dl.main(argv)
    return rc, out.getvalue(), err.getvalue()


class DenyListCoreTests(unittest.TestCase):
    """DenyList: add/remove/list, matching, reload, atomic write, corruption."""

    def test_add_remove_list_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "denylist.json"
            d = dl.DenyList(path)
            self.assertEqual(d.load(), [])

            entry = d.add(zone="datadoghq.com", scope="global", reason="telemetry", now=NOW)
            self.assertEqual(entry.zone, "datadoghq.com")
            self.assertEqual(entry.scope, "global")
            self.assertEqual(entry.reason, "telemetry")
            self.assertEqual(entry.created_at, "2026-08-27T21:40:00Z")

            loaded = d.load()
            self.assertEqual(len(loaded), 1)
            self.assertEqual(loaded[0], entry)

            self.assertTrue(d.remove(zone="datadoghq.com", scope="global"))
            self.assertEqual(d.load(), [])
            self.assertFalse(d.remove(zone="datadoghq.com", scope="global"))

    def test_add_replaces_existing_same_zone_scope_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = dl.DenyList(Path(tmp) / "denylist.json")
            d.add(zone="example.net", scope="global", reason="first")
            d.add(zone="example.net", scope="global", reason="second")
            entries = d.load()
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0].reason, "second")

    def test_on_disk_schema_matches_pln(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "denylist.json"
            d = dl.DenyList(path)
            d.add(zone="datadoghq.com", scope="global", reason="claude telemetry", now=NOW)
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["version"], 1)
            self.assertEqual(len(payload["entries"]), 1)
            entry = payload["entries"][0]
            self.assertEqual(entry["zone"], "datadoghq.com")
            self.assertEqual(entry["scope"], "global")
            self.assertEqual(entry["reason"], "claude telemetry")
            self.assertEqual(entry["created_at"], "2026-08-27T21:40:00Z")
            self.assertEqual(entry["by"], "operator")

    def test_matches_global_scope_covers_every_bottle(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = dl.DenyList(Path(tmp) / "denylist.json")
            d.add(zone="datadoghq.com", scope="global")
            self.assertIsNotNone(d.matches("coding-brassbottle", "datadoghq.com"))
            self.assertIsNotNone(d.matches("some-other-bottle", "datadoghq.com"))

    def test_matches_bottle_scope_only_covers_that_bottle(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = dl.DenyList(Path(tmp) / "denylist.json")
            d.add(zone="example.net", scope="coding-hank")
            self.assertIsNotNone(d.matches("coding-hank", "example.net"))
            self.assertIsNone(d.matches("coding-brassbottle", "example.net"))

    def test_matches_zone_suffix_covers_subdomains(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = dl.DenyList(Path(tmp) / "denylist.json")
            d.add(zone="datadoghq.com", scope="global")
            entry = d.matches("any-bottle", "http-intake.logs.us5.datadoghq.com")
            self.assertIsNotNone(entry)
            self.assertEqual(entry.zone, "datadoghq.com")
            self.assertIsNone(d.matches("any-bottle", "notdatadoghq.com"))

    def test_matches_ip_literal_is_exact_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = dl.DenyList(Path(tmp) / "denylist.json")
            d.add(zone="192.0.2.55", scope="global")
            self.assertIsNotNone(d.matches("any-bottle", "192.0.2.55"))
            # No CIDR/subdomain semantics for IP literals (PLN: "no CIDR deny").
            self.assertIsNone(d.matches("any-bottle", "192.0.2.56"))

    def test_matches_longest_zone_wins_for_reporting(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = dl.DenyList(Path(tmp) / "denylist.json")
            d.add(zone="stripe.com", scope="global", reason="broad")
            d.add(zone="docs.stripe.com", scope="global", reason="narrow")
            entry = d.matches("any-bottle", "docs.stripe.com")
            self.assertEqual(entry.zone, "docs.stripe.com")
            self.assertEqual(entry.reason, "narrow")

    def test_matches_tie_break_prefers_bottle_scope_global_added_first(self):
        """finding #5: on an equal-length zone tie, the bottle-scoped entry
        wins over global — deterministically, not by whichever happened to
        be added (and so loaded) first. This order: global added first."""
        with tempfile.TemporaryDirectory() as tmp:
            d = dl.DenyList(Path(tmp) / "denylist.json")
            d.add(zone="example.com", scope="global", reason="global-broad")
            d.add(zone="example.com", scope="coding-hank", reason="bottle-specific")
            entry = d.matches("coding-hank", "example.com")
            self.assertEqual(entry.scope, "coding-hank")
            self.assertEqual(entry.reason, "bottle-specific")

    def test_matches_tie_break_prefers_bottle_scope_bottle_added_first(self):
        """Same tie, opposite insertion order: bottle-scoped added first —
        must still win, proving the tie-break is order-independent."""
        with tempfile.TemporaryDirectory() as tmp:
            d = dl.DenyList(Path(tmp) / "denylist.json")
            d.add(zone="example.com", scope="coding-hank", reason="bottle-specific")
            d.add(zone="example.com", scope="global", reason="global-broad")
            entry = d.matches("coding-hank", "example.com")
            self.assertEqual(entry.scope, "coding-hank")
            self.assertEqual(entry.reason, "bottle-specific")

    def test_no_match_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = dl.DenyList(Path(tmp) / "denylist.json")
            self.assertIsNone(d.matches("any-bottle", "example.com"))

    def test_mtime_reload_picks_up_external_edit(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "denylist.json"
            reader = dl.DenyList(path)
            self.assertIsNone(reader.matches("any-bottle", "example.net"))

            writer = dl.DenyList(path)
            writer.add(zone="example.net", scope="global")

            # `reader` never called add()/load() itself; matches() alone must
            # observe the on-disk change made by a separate process (mirrors
            # BottleTokenStore._reload — no daemon restart required).
            entry = reader.matches("any-bottle", "example.net")
            self.assertIsNotNone(entry)
            self.assertEqual(entry.zone, "example.net")

    def test_corrupt_file_treated_as_empty_and_logged(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "denylist.json"
            path.write_text("{not valid json", encoding="utf-8")
            with self.assertLogs(dl.LOG, level="INFO") as captured:
                d = dl.DenyList(path)
            self.assertEqual(d.load(), [])
            self.assertTrue(
                any("unreadable" in r.getMessage() for r in captured.records)
            )

    def test_corrupt_file_matches_logs_warning_not_just_info(self):
        """finding #3: matches() (a read that decides real behaviour, unlike
        the load-time diagnosis) must complain LOUDER than INFO — a corrupt
        denylist silently means nothing is denied any more."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "denylist.json"
            path.write_text("{not valid json", encoding="utf-8")
            d = dl.DenyList(path)
            with self.assertLogs(dl.LOG, level="WARNING") as captured:
                result = d.matches("any-bottle", "example.com")
            self.assertIsNone(result)
            self.assertTrue(
                any(r.levelno == logging.WARNING for r in captured.records)
            )

    def test_corrupt_file_matches_warning_gated_to_once_per_mtime(self):
        """Cleanup (finding C): matches() is called on EVERY egress request
        — logging the corrupt-file WARNING on every one of those, forever,
        is a louder version of the exact log flood the short-circuit
        coalesce window exists to prevent. Gate it to once per distinct
        on-disk mtime: log again only when the file actually changes (still
        corrupt or not)."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "denylist.json"
            path.write_text("{not valid json", encoding="utf-8")
            d = dl.DenyList(path)

            with self.assertLogs(dl.LOG, level="WARNING") as captured:
                for _ in range(5):
                    self.assertIsNone(d.matches("any-bottle", "example.com"))
            self.assertEqual(len(captured.records), 1)

            # Touch the file (new mtime, same content) — warns again exactly
            # once, not once per subsequent consult.
            time.sleep(0.01)
            path.write_text("{still not valid json", encoding="utf-8")
            with self.assertLogs(dl.LOG, level="WARNING") as captured2:
                for _ in range(5):
                    self.assertIsNone(d.matches("any-bottle", "example.com"))
            self.assertEqual(len(captured2.records), 1)

    def test_corrupt_file_refuses_add_and_leaves_file_byte_for_byte_unchanged(self):
        """finding #3: a corrupt file must never be silently overwritten by
        the next add() — that would destroy every prior entry."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "denylist.json"
            original = '{"version": 1, "entries": [BROKEN'
            path.write_text(original, encoding="utf-8")
            d = dl.DenyList(path)
            with self.assertRaises(dl.DenyListError) as ctx:
                d.add(zone="new.example.com", scope="global")
            self.assertIn("unreadable", str(ctx.exception))
            self.assertIn("refusing to overwrite", str(ctx.exception))
            self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_corrupt_file_refuses_remove_and_leaves_file_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "denylist.json"
            original = "not json at all"
            path.write_text(original, encoding="utf-8")
            d = dl.DenyList(path)
            with self.assertRaises(dl.DenyListError):
                d.remove(zone="new.example.com", scope="global")
            self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_denylist_add_is_serialized_across_two_instances_by_flock(self):
        """finding #10: DenyList.add()/remove() hold an exclusive flock on a
        sibling denylist.lock across load->mutate->os.replace, so the daemon
        process and a `./djinn deny` CLI process cannot interleave a
        read-modify-write and drop one or the other's entry.

        Proven by making one instance's write PAUSE mid-critical-section
        (inside the locked region) and showing a second instance's add() —
        on a DIFFERENT DenyList object, a different open() of the same
        lock file — genuinely blocks until the first releases, rather than
        reading stale state and clobbering it. Both entries must survive.
        """
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "denylist.json"
            d1 = dl.DenyList(path)
            d2 = dl.DenyList(path)

            entered_critical_section = threading.Event()
            release_d1 = threading.Event()
            original_persist = dl.DenyList._persist

            def slow_persist(self, entries):
                if self is d1:
                    entered_critical_section.set()
                    release_d1.wait(timeout=5)
                return original_persist(self, entries)

            with mock.patch.object(dl.DenyList, "_persist", slow_persist):
                t1 = threading.Thread(
                    target=lambda: d1.add(zone="a.example.com", scope="global")
                )
                t1.start()
                self.assertTrue(entered_critical_section.wait(timeout=5))

                t2 = threading.Thread(
                    target=lambda: d2.add(zone="b.example.com", scope="global")
                )
                t2.start()
                time.sleep(0.2)
                self.assertTrue(
                    t2.is_alive(),
                    "second add() must block on the flock, not race ahead "
                    "while the first is still mid-write",
                )

                release_d1.set()
                t1.join(timeout=5)
                t2.join(timeout=5)

            entries = {e.zone for e in dl.DenyList(path).load()}
            self.assertEqual(entries, {"a.example.com", "b.example.com"})

    def test_non_object_json_treated_as_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "denylist.json"
            path.write_text("[1, 2, 3]", encoding="utf-8")
            d = dl.DenyList(path)
            self.assertEqual(d.load(), [])

    def test_missing_file_is_empty_not_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = dl.DenyList(Path(tmp) / "nope" / "denylist.json")
            self.assertEqual(d.load(), [])
            self.assertIsNone(d.matches("any-bottle", "example.com"))

    def test_malformed_entries_are_skipped_not_fatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "denylist.json"
            path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "entries": [
                            {"zone": "good.com", "scope": "global", "created_at": "x"},
                            {"scope": "global"},  # missing zone
                            "not-a-dict",
                            42,
                        ],
                    }
                ),
                encoding="utf-8",
            )
            d = dl.DenyList(path)
            entries = d.load()
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0].zone, "good.com")

    def test_atomic_write_leaves_original_untouched_on_replace_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "denylist.json"
            d = dl.DenyList(path)
            d.add(zone="stable.example.com", scope="global")
            original_bytes = path.read_bytes()

            with mock.patch("os.replace", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    d.add(zone="new.example.com", scope="global")

            # The destination must never be left partially written.
            self.assertEqual(path.read_bytes(), original_bytes)
            # No stray temp file left behind either — denylist.lock is the
            # expected sibling lock file (finding #10), not a leftover.
            leftovers = [
                p for p in Path(tmp).iterdir()
                if p.name not in ("denylist.json", dl.DENYLIST_LOCK_FILENAME)
            ]
            self.assertEqual(leftovers, [], f"stray files: {leftovers}")

            # A fresh DenyList still sees only the entry that was durably
            # written before the failed add.
            reread = dl.DenyList(path).load()
            self.assertEqual([e.zone for e in reread], ["stable.example.com"])

    def test_validate_bottle_scope_rejects_global_as_a_bottle_name(self):
        """finding #4: "global" is a reserved scope name, not a bottle name.
        validate_bottle_scope is ONLY ever called with an actual bottle name
        (callers skip the call entirely for a true global-scope write — see
        _cmd_add and persist_deny), so a caller reaching it with "global"
        means a bottle literally trying to name itself "global" — reject it
        BEFORE the token-file check runs at all (no token dir needed here to
        prove that: the raise happens before tokens_dir is ever touched)."""
        with self.assertRaises(dl.DenyListError) as ctx:
            dl.validate_bottle_scope("global", Path("/does/not/exist/tokens"))
        self.assertIn("reserved", str(ctx.exception))

    def test_validate_bottle_scope_rejects_unknown_bottle(self):
        with tempfile.TemporaryDirectory() as tmp:
            tokens_dir = Path(tmp) / "tokens"
            tokens_dir.mkdir()
            with self.assertRaises(dl.DenyListError):
                dl.validate_bottle_scope("ghost-bottle", tokens_dir)

    def test_validate_bottle_scope_accepts_known_bottle(self):
        with tempfile.TemporaryDirectory() as tmp:
            tokens_dir = Path(tmp) / "tokens"
            tokens_dir.mkdir()
            (tokens_dir / "coding-hank.token").write_text("tok\n", encoding="utf-8")
            dl.validate_bottle_scope("coding-hank", tokens_dir)  # no raise

    def test_host_covered_by_zone_matches_egress_broker_host_semantics(self):
        self.assertTrue(dl.host_covered_by_zone("docs.stripe.com", "stripe.com"))
        self.assertTrue(dl.host_covered_by_zone("stripe.com", "stripe.com"))
        self.assertFalse(dl.host_covered_by_zone("notstripe.com", "stripe.com"))


class DenyListCliTests(unittest.TestCase):
    """CLI surface: add / remove / list / --check, as `./djinn deny|undeny` exec it."""

    def _base(self, tmp: str) -> Path:
        return Path(tmp) / "home"

    def test_add_requires_exactly_one_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = self._base(tmp)
            rc, _out, err = _run_main(["add", "foo.com", "--base-path", str(base)])
            self.assertEqual(rc, dl.EXIT_USAGE_ERROR)
            self.assertIn("scope is required", err)

            rc, _out, err = _run_main(
                ["add", "foo.com", "--bottle", "x", "--global", "--base-path", str(base)]
            )
            self.assertEqual(rc, dl.EXIT_USAGE_ERROR)
            self.assertIn("exactly one", err)

    def test_add_rejects_unknown_bottle(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = self._base(tmp)
            rc, _out, err = _run_main(
                ["add", "foo.com", "--bottle", "ghost", "--base-path", str(base)]
            )
            self.assertEqual(rc, dl.EXIT_USAGE_ERROR)
            self.assertIn("unknown bottle", err)
            # Hard error: nothing written.
            self.assertFalse((base / "run" / "egress" / dl.DENYLIST_FILENAME).exists())

    def test_add_bottle_global_is_rejected_not_aliased_to_global_scope(self):
        """finding #4: `./djinn deny X --bottle global` must be rejected —
        never silently treated as `--global` just because the strings
        match. No daemon running here (no operator.token), so this also
        proves the rejection happens on the direct-write validation path,
        before any daemon POST is even attempted."""
        with tempfile.TemporaryDirectory() as tmp:
            base = self._base(tmp)
            rc, _out, err = _run_main(
                ["add", "foo.com", "--bottle", "global", "--base-path", str(base)]
            )
            self.assertEqual(rc, dl.EXIT_USAGE_ERROR)
            self.assertIn("reserved", err)
            self.assertFalse((base / "run" / "egress" / dl.DENYLIST_FILENAME).exists())

    def test_add_global_then_list_then_undeny(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = self._base(tmp)
            rc, out, _err = _run_main(
                ["add", "datadoghq.com", "--global", "--reason", "telemetry", "--base-path", str(base)]
            )
            self.assertEqual(rc, 0)
            # finding #8: _format_denied's full line, not a substring — the
            # daemon-success and direct-write paths must produce this exact
            # wording, byte-identical.
            self.assertIn("Denied: datadoghq.com (scope=global) — telemetry", out)

            rc, out, _err = _run_main(["list", "--base-path", str(base)])
            self.assertEqual(rc, 0)
            self.assertIn("datadoghq.com", out)
            self.assertIn("scope=global", out)
            self.assertIn("reason=telemetry", out)

            rc, out, _err = _run_main(
                ["remove", "datadoghq.com", "--global", "--base-path", str(base)]
            )
            self.assertEqual(rc, 0)
            self.assertIn("Undenied", out)

            rc, out, _err = _run_main(["list", "--base-path", str(base)])
            self.assertEqual(rc, 0)
            self.assertIn("no denylist entries", out)

    def test_remove_nonexistent_entry_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = self._base(tmp)
            rc, _out, err = _run_main(
                ["remove", "nope.com", "--global", "--base-path", str(base)]
            )
            self.assertEqual(rc, dl.EXIT_NOT_FOUND)
            self.assertIn("No denylist entry", err)

    def test_remove_bottle_global_is_rejected_not_aliased_to_global_scope(self):
        """finding #2: `./djinn undeny X --bottle global` must be rejected
        the same way `./djinn deny X --bottle global` is — never silently
        treated as `--global` and never allowed to delete the GLOBAL entry.
        Seed a real global entry first so a bug here would be observable
        (the entry surviving is not enough on its own — the exit code and
        message must show the write was refused, not merely a no-op)."""
        with tempfile.TemporaryDirectory() as tmp:
            base = self._base(tmp)
            _run_main(
                ["add", "foo.com", "--global", "--base-path", str(base)]
            )
            rc, _out, err = _run_main(
                ["remove", "foo.com", "--bottle", "global", "--base-path", str(base)]
            )
            self.assertEqual(rc, dl.EXIT_USAGE_ERROR)
            self.assertIn("reserved", err)

            rc, out, _err = _run_main(["list", "--base-path", str(base)])
            self.assertEqual(rc, 0)
            self.assertIn("foo.com", out)
            self.assertIn("scope=global", out)

    def test_remove_rejects_unknown_bottle(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = self._base(tmp)
            rc, _out, err = _run_main(
                ["remove", "foo.com", "--bottle", "ghost", "--base-path", str(base)]
            )
            self.assertEqual(rc, dl.EXIT_USAGE_ERROR)
            self.assertIn("unknown bottle", err)

    def test_remove_accepts_known_bottle(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = self._base(tmp)
            tokens_dir = base / "run" / "egress" / "tokens"
            tokens_dir.mkdir(parents=True)
            (tokens_dir / "coding-hank.token").write_text("tok\n", encoding="utf-8")
            _run_main(
                ["add", "example.net", "--bottle", "coding-hank", "--base-path", str(base)]
            )

            rc, out, _err = _run_main(
                ["remove", "example.net", "--bottle", "coding-hank", "--base-path", str(base)]
            )
            self.assertEqual(rc, 0)
            self.assertIn("Undenied", out)

    def test_add_bottle_scope_requires_existing_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = self._base(tmp)
            tokens_dir = base / "run" / "egress" / "tokens"
            tokens_dir.mkdir(parents=True)
            (tokens_dir / "coding-hank.token").write_text("tok\n", encoding="utf-8")

            rc, out, _err = _run_main(
                ["add", "example.net", "--bottle", "coding-hank", "--base-path", str(base)]
            )
            self.assertEqual(rc, 0)
            self.assertIn("scope=coding-hank", out)

    def test_normalize_zone_domain_unchanged(self):
        self.assertEqual(dl._normalize_zone("*.neon.tech"), "neon.tech")

    def test_normalize_zone_accepts_ip_with_port(self):
        self.assertEqual(dl._normalize_zone("93.0.2.55:443"), "93.0.2.55")

    def test_normalize_zone_accepts_ip_with_scheme(self):
        self.assertEqual(dl._normalize_zone("http://93.0.2.55"), "93.0.2.55")

    def test_normalize_zone_accepts_bare_ip(self):
        self.assertEqual(dl._normalize_zone("93.0.2.55"), "93.0.2.55")

    def test_normalize_zone_accepts_bracketed_ipv6_with_port(self):
        self.assertEqual(dl._normalize_zone("[::1]:443"), "::1")

    def test_normalize_zone_rejects_garbage(self):
        with self.assertRaises(dl.DenyListError):
            dl._normalize_zone("not a zone at all!!")

    def test_add_oserror_on_readonly_dir_reports_error_and_exits_corrupt(self):
        """finding B3: OSError from a failed write (e.g. can't create the
        flock file under a read-only directory) must be caught like
        DenyListError — an "Error: ..." message and a clean exit, never an
        uncaught traceback."""
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.skipTest("root bypasses permission checks")
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "home"
            run_dir = base / "run" / "egress"
            run_dir.mkdir(parents=True)
            os.chmod(run_dir, 0o500)
            try:
                rc, _out, err = _run_main(
                    ["add", "example.com", "--global", "--base-path", str(base)]
                )
            finally:
                os.chmod(run_dir, 0o700)
            self.assertEqual(rc, dl.EXIT_CORRUPT)
            self.assertIn("Error:", err)

    def test_remove_oserror_on_readonly_dir_reports_error_and_exits_corrupt(self):
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.skipTest("root bypasses permission checks")
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "home"
            run_dir = base / "run" / "egress"
            run_dir.mkdir(parents=True)
            # Seed an entry (writably) before locking the directory down, so
            # remove() has something to find before it tries — and fails —
            # to take the flock.
            os.chmod(run_dir, 0o700)
            denylist, _root = dl._denylist_for(base)
            denylist.add(zone="example.com", scope="global")
            os.chmod(run_dir, 0o500)
            try:
                rc, _out, err = _run_main(
                    ["remove", "example.com", "--global", "--base-path", str(base)]
                )
            finally:
                os.chmod(run_dir, 0o700)
            self.assertEqual(rc, dl.EXIT_CORRUPT)
            self.assertIn("Error:", err)

    def test_check_prints_warning_for_covered_domain_and_stays_silent_for_uncovered(self):
        """finding #10: --check now prints its own operator-facing warning
        line for a covered domain (bash used to compose this itself from
        the bare entry text --check used to print) and exits 0 either way —
        coverage is signalled by what got printed, not the exit code."""
        with tempfile.TemporaryDirectory() as tmp:
            base = self._base(tmp)
            _run_main(["add", "datadoghq.com", "--global", "--base-path", str(base)])

            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = dl._cmd_check(["any-bottle", "us5.datadoghq.com", "--base-path", str(base)])
            self.assertEqual(rc, 0)
            self.assertIn(
                "⚠ us5.datadoghq.com is covered by a persistent deny-list entry: "
                "datadoghq.com (scope=global)",
                out.getvalue(),
            )

            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = dl._cmd_check(["any-bottle", "example.com", "--base-path", str(base)])
            self.assertEqual(rc, 0)
            self.assertEqual(out.getvalue(), "")

    def test_check_accepts_multiple_domains_in_one_call(self):
        """finding #10: the loop moved from bash into Python — one call
        checks every domain, printing one line per covered domain."""
        with tempfile.TemporaryDirectory() as tmp:
            base = self._base(tmp)
            _run_main(["add", "datadoghq.com", "--global", "--base-path", str(base)])

            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = dl._cmd_check(
                    [
                        "any-bottle",
                        "us5.datadoghq.com",
                        "example.com",
                        "other.datadoghq.com",
                        "--base-path",
                        str(base),
                    ]
                )
            self.assertEqual(rc, 0)
            lines = [l for l in out.getvalue().splitlines() if l]
            self.assertEqual(len(lines), 2)
            self.assertIn("us5.datadoghq.com is covered", lines[0])
            self.assertIn("other.datadoghq.com is covered", lines[1])
            self.assertNotIn("example.com", out.getvalue())

    def test_check_corrupt_file_exits_check_corrupt_with_diagnosis_on_stderr(self):
        """finding #8a: a corrupt denylist.json must not read as "nothing is
        covered" — --check exits EXIT_CHECK_CORRUPT (4), not 0/3, with the
        diagnosis on stderr, so bin/allow-egress.sh's `|| ...` fallback
        surfaces it as a named failure instead of silently checking clean."""
        with tempfile.TemporaryDirectory() as tmp:
            base = self._base(tmp)
            path = base / "run" / "egress" / dl.DENYLIST_FILENAME
            path.parent.mkdir(parents=True)
            path.write_text("{BROKEN", encoding="utf-8")

            out = io.StringIO()
            err = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = dl._cmd_check(["any-bottle", "example.com", "--base-path", str(base)])
            self.assertEqual(rc, dl.EXIT_CHECK_CORRUPT)
            self.assertIn("unreadable", err.getvalue())
            self.assertEqual(out.getvalue(), "")

    def test_check_top_level_dispatch_via_main(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = self._base(tmp)
            _run_main(["add", "datadoghq.com", "--global", "--base-path", str(base)])
            rc, out, _err = _run_main(
                ["--check", "any-bottle", "us5.datadoghq.com", "--base-path", str(base)]
            )
            self.assertEqual(rc, 0)
            self.assertIn("datadoghq.com", out)

    def test_exit_codes_are_pairwise_distinct_for_usage_vs_not_found(self):
        """Cleanup: EXIT_USAGE_ERROR and EXIT_NOT_FOUND used to both be 1,
        so a bare `./djinn undeny X --bottle Y` (nonexistent entry) and a
        `./djinn deny X` (missing scope) were indistinguishable by exit code
        alone. EXIT_NOT_FOUND now matches EXIT_CHECK_NOT_COVERED (both mean
        "no matching entry")."""
        self.assertNotEqual(dl.EXIT_USAGE_ERROR, dl.EXIT_NOT_FOUND)
        self.assertEqual(dl.EXIT_NOT_FOUND, dl.EXIT_CHECK_NOT_COVERED)
        self.assertEqual(dl.EXIT_USAGE_ERROR, 2)
        self.assertEqual(dl.EXIT_NOT_FOUND, 3)
        self.assertEqual(dl.EXIT_CORRUPT, 1)

    def test_add_on_corrupt_file_exits_corrupt_and_leaves_file_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = self._base(tmp)
            path = base / "run" / "egress" / dl.DENYLIST_FILENAME
            path.parent.mkdir(parents=True)
            original = "{BROKEN"
            path.write_text(original, encoding="utf-8")

            rc, _out, err = _run_main(
                ["add", "example.com", "--global", "--base-path", str(base)]
            )
            self.assertEqual(rc, dl.EXIT_CORRUPT)
            self.assertIn("unreadable", err)
            self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_remove_on_corrupt_file_exits_corrupt(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = self._base(tmp)
            path = base / "run" / "egress" / dl.DENYLIST_FILENAME
            path.parent.mkdir(parents=True)
            path.write_text("{BROKEN", encoding="utf-8")

            rc, _out, err = _run_main(
                ["remove", "example.com", "--global", "--base-path", str(base)]
            )
            self.assertEqual(rc, dl.EXIT_CORRUPT)
            self.assertIn("unreadable", err)

    def test_list_on_corrupt_file_prints_diagnosis_and_exits_corrupt(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = self._base(tmp)
            path = base / "run" / "egress" / dl.DENYLIST_FILENAME
            path.parent.mkdir(parents=True)
            path.write_text("{BROKEN", encoding="utf-8")

            rc, _out, err = _run_main(["list", "--base-path", str(base)])
            self.assertEqual(rc, dl.EXIT_CORRUPT)
            self.assertIn("unreadable", err)


class CliDaemonFirstAddTests(unittest.TestCase):
    """`./djinn deny` tries the daemon FIRST (a single POST /decide carrying
    the REAL scope) and only falls back to writing denylist.json directly
    when the daemon can't be reached at all. Supersedes the old
    write-then-notify-scope-once flow (NotifyDaemonTests, removed): the
    daemon-side persist_deny() now does its own sweep under lock in the same
    call, so the CLI no longer fans out one /decide POST per bottle itself
    — undeny (`_cmd_remove`) still never talks to the daemon, since removing
    an entry doesn't need to release anything currently held."""

    def _base(self, tmp: str) -> Path:
        return Path(tmp) / "home"

    def _start_server(self, handler_cls) -> tuple[HTTPServer, threading.Thread, int]:
        server = HTTPServer(("127.0.0.1", 0), handler_cls)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        host, port = server.server_address
        wait_for_tcp_listening(host, port)
        return server, thread, port

    def _seed_daemon(self, egress_root: Path, port: int, *, token: str = "secret") -> None:
        """Seed operator.token + daemon.json (host 127.0.0.1, this test
        process's own always-alive pid) — the single source of truth
        _try_daemon_deny now reads via egress_broker_host.daemon_base_url,
        replacing the old config.json {"port": ...} scheme."""
        egress_root.mkdir(parents=True, exist_ok=True)
        (egress_root / "operator.token").write_text(f"{token}\n", encoding="utf-8")
        broker_host.write_daemon_endpoint(egress_root, "127.0.0.1", port)

    def _dead_pid(self) -> int:
        """A pid guaranteed not to be alive — see the identical helper in
        tests/test_egress_broker_host.py for why wait()-then-reuse is safe."""
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        return proc.pid

    def test_try_daemon_deny_no_operator_token_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            egress_root = Path(tmp)
            with self.assertLogs(dl.LOG, level="INFO") as captured:
                result = dl._try_daemon_deny(
                    egress_root, zone="example.com", scope="global", container=None, reason=None
                )
            self.assertIsNone(result)
            self.assertTrue(
                any("no_operator_token" in r.getMessage() for r in captured.records)
            )

    def test_try_daemon_deny_unreachable_returns_none_and_logs_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            egress_root = Path(tmp)
            # daemon.json points at port 1 (guaranteed refused) — the
            # single source of truth _try_daemon_deny reads now; config.json
            # is no longer consulted at all.
            self._seed_daemon(egress_root, 1)
            with self.assertLogs(dl.LOG, level="INFO") as captured:
                result = dl._try_daemon_deny(
                    egress_root, zone="example.com", scope="global", container=None, reason=None
                )
            self.assertIsNone(result)
            self.assertTrue(
                any(
                    "status=unreachable" in r.getMessage() and "duration_ms=" in r.getMessage()
                    for r in captured.records
                )
            )

    def test_try_daemon_deny_success_logs_status_and_duration(self):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a, **k):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                self.rfile.read(length)
                body = b'{"decided":[],"persisted":{"zone":"x.com","scope":"global"}}'
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server, thread, port = self._start_server(Handler)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                egress_root = Path(tmp)
                self._seed_daemon(egress_root, port)
                with self.assertLogs(dl.LOG, level="INFO") as captured:
                    result = dl._try_daemon_deny(
                        egress_root, zone="x.com", scope="global", container=None, reason=None
                    )
        finally:
            server.shutdown()
            thread.join(timeout=5)

        self.assertIsNotNone(result)
        status, body = result
        self.assertEqual(status, 200)
        self.assertEqual(body["persisted"], {"zone": "x.com", "scope": "global"})
        self.assertTrue(
            any(
                "status=200" in r.getMessage() and "duration_ms=" in r.getMessage()
                for r in captured.records
            )
        )

    def test_try_daemon_deny_targets_daemon_json_endpoint_not_a_hardcoded_default(self):
        """finding (PR #85 review, daemon endpoint): _try_daemon_deny must
        read $egress_root/daemon.json (via egress_broker_host.daemon_base_url)
        rather than assuming 127.0.0.1:DEFAULT_PORT — proven by binding the
        test server to an ephemeral, DEFINITELY-not-default port and only
        pointing at it via daemon.json (never via config.json, which no
        longer exists in this scheme)."""
        received: list[dict] = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a, **k):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                received.append(json.loads(self.rfile.read(length).decode("utf-8")))
                body = b'{"decided":[]}'
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server, thread, port = self._start_server(Handler)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                egress_root = Path(tmp)
                egress_root.mkdir(parents=True, exist_ok=True)
                (egress_root / "operator.token").write_text("tok\n", encoding="utf-8")
                broker_host.write_daemon_endpoint(egress_root, "127.0.0.1", port)
                result = dl._try_daemon_deny(
                    egress_root,
                    zone="daemon-json.example",
                    scope="global",
                    container=None,
                    reason=None,
                )
        finally:
            server.shutdown()
            thread.join(timeout=5)

        self.assertIsNotNone(result)
        status, _body = result
        self.assertEqual(status, 200)
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0]["host"], "daemon-json.example")

    def test_add_success_via_daemon_prints_persisted_and_decided_no_local_write(self):
        received: list[dict] = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a, **k):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                received.append(json.loads(self.rfile.read(length).decode("utf-8")))
                body = json.dumps(
                    {
                        "decided": ["aaaaaaaa", "bbbbbbbb"],
                        "persisted": {"zone": "datadoghq.com", "scope": "global"},
                    }
                ).encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server, thread, port = self._start_server(Handler)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                base = self._base(tmp)
                egress_root = base / "run" / "egress"
                self._seed_daemon(egress_root, port)
                rc, out, _err = _run_main(
                    [
                        "add", "datadoghq.com", "--global", "--reason", "telemetry",
                        "--base-path", str(base),
                    ]
                )
                # The daemon answered 200 — it wrote the entry under its own
                # lock; nothing local should have been written from here.
                self.assertFalse((egress_root / dl.DENYLIST_FILENAME).exists())
        finally:
            server.shutdown()
            thread.join(timeout=5)

        self.assertEqual(rc, 0)
        # finding #8: full line (zone, scope, reason) — not a substring.
        self.assertIn("Denied: datadoghq.com (scope=global) — telemetry", out)
        self.assertIn("Closed 2 open request", out)
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0]["decision"], "deny")
        self.assertEqual(received[0]["scope"], "global")
        self.assertEqual(received[0]["host"], "datadoghq.com")
        self.assertEqual(received[0]["reason"], "telemetry")
        self.assertNotIn("container", received[0])

    def test_add_bottle_scope_sends_container_field_to_daemon(self):
        received: list[dict] = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a, **k):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                received.append(json.loads(self.rfile.read(length).decode("utf-8")))
                body = json.dumps(
                    {"decided": [], "persisted": {"zone": "example.net", "scope": "coding-hank"}}
                ).encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server, thread, port = self._start_server(Handler)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                base = self._base(tmp)
                egress_root = base / "run" / "egress"
                tokens_dir = egress_root / "tokens"
                tokens_dir.mkdir(parents=True)
                (tokens_dir / "coding-hank.token").write_text("tok\n", encoding="utf-8")
                self._seed_daemon(egress_root, port)

                rc, out, _err = _run_main(
                    ["add", "example.net", "--bottle", "coding-hank", "--base-path", str(base)]
                )
        finally:
            server.shutdown()
            thread.join(timeout=5)

        self.assertEqual(rc, 0)
        self.assertIn("scope=coding-hank", out)
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0]["scope"], "bottle")
        self.assertEqual(received[0]["container"], "coding-hank")

    def test_add_bottle_scope_daemon_reachable_bottle_unknown_locally_daemon_answer_wins(self):
        """finding #3: _cmd_add asks the daemon FIRST and only runs the
        local validate_bottle_scope guard on the fallback path — so a
        DJINN_HOME divergence between this CLI process and the running
        daemon cannot make the CLI reject a bottle the daemon knows about.
        Deliberately no tokens/coding-hank.token under this base_path (the
        local check would reject "unknown bottle" if it ran first); the
        daemon answers 200 anyway, proving its answer wins."""
        received: list[dict] = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a, **k):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                received.append(json.loads(self.rfile.read(length).decode("utf-8")))
                body = json.dumps(
                    {"decided": [], "persisted": {"zone": "example.net", "scope": "coding-hank"}}
                ).encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server, thread, port = self._start_server(Handler)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                base = self._base(tmp)
                egress_root = base / "run" / "egress"
                # No tokens/ dir at all under this base_path — a local
                # validate_bottle_scope would raise "unknown bottle" if it
                # ran before the daemon call.
                self._seed_daemon(egress_root, port)

                rc, out, _err = _run_main(
                    ["add", "example.net", "--bottle", "coding-hank", "--base-path", str(base)]
                )
                self.assertFalse((egress_root / dl.DENYLIST_FILENAME).exists())
        finally:
            server.shutdown()
            thread.join(timeout=5)

        self.assertEqual(rc, 0)
        self.assertIn("scope=coding-hank", out)
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0]["container"], "coding-hank")

    def test_add_daemon_400_is_a_hard_error_no_fallback_write(self):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a, **k):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                self.rfile.read(length)
                body = b'{"error":"unknown bottle \'ghost\'"}'
                self.send_response(HTTPStatus.BAD_REQUEST)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server, thread, port = self._start_server(Handler)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                base = self._base(tmp)
                egress_root = base / "run" / "egress"
                self._seed_daemon(egress_root, port)

                rc, _out, err = _run_main(
                    ["add", "datadoghq.com", "--global", "--base-path", str(base)]
                )
                self.assertFalse((egress_root / dl.DENYLIST_FILENAME).exists())
        finally:
            server.shutdown()
            thread.join(timeout=5)

        self.assertEqual(rc, dl.EXIT_USAGE_ERROR)
        self.assertIn("unknown bottle", err)

    def test_add_daemon_non_400_error_status_is_a_hard_error_no_fallback_write(self):
        """finding #3: ANY HTTP status the daemon actually answered that is
        not 200 is a hard error — never a silent fallback to a direct file
        write. Only a connection-level failure (unreachable) or no operator
        token yet may fall back; the daemon here is reachable and answered,
        it just refused (401 = bad/rotated bearer token, 403 = forbidden,
        500 = its own internal error) — falling back would write the entry
        UNDER the daemon's nose while it's up, and skip the sweep of any
        held requests only persist_deny() can do."""
        for status, error_text in (
            (HTTPStatus.UNAUTHORIZED, "bad token"),
            (HTTPStatus.FORBIDDEN, "forbidden"),
            (HTTPStatus.INTERNAL_SERVER_ERROR, "boom"),
        ):
            with self.subTest(status=status):
                class Handler(BaseHTTPRequestHandler):
                    def log_message(self, *a, **k):
                        pass

                    def do_POST(self):
                        length = int(self.headers.get("Content-Length", "0"))
                        self.rfile.read(length)
                        body = json.dumps({"error": error_text}).encode("utf-8")
                        self.send_response(status)
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)

                server, thread, port = self._start_server(Handler)
                try:
                    with tempfile.TemporaryDirectory() as tmp:
                        base = self._base(tmp)
                        egress_root = base / "run" / "egress"
                        self._seed_daemon(egress_root, port)

                        rc, _out, err = _run_main(
                            ["add", "datadoghq.com", "--global", "--base-path", str(base)]
                        )
                        self.assertFalse(
                            (egress_root / dl.DENYLIST_FILENAME).exists(),
                            f"status {status}: must not fall back to a direct write",
                        )
                finally:
                    server.shutdown()
                    thread.join(timeout=5)

                self.assertEqual(rc, dl.EXIT_CORRUPT)
                self.assertIn(f"daemon answered {int(status)}", err)
                self.assertIn(error_text, err)
                self.assertNotIn("daemon not reachable", err)

    def test_add_daemon_unreachable_falls_back_to_direct_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = self._base(tmp)
            egress_root = base / "run" / "egress"
            # Port 1: nothing listens there, so the POST fails at the
            # connection level (refused) rather than timing out.
            self._seed_daemon(egress_root, 1)

            rc, out, err = _run_main(
                ["add", "datadoghq.com", "--global", "--base-path", str(base)]
            )
            self.assertEqual(rc, 0)
            # finding #8: same full "Denied: ..." line as the daemon-success
            # path above (no reason here, so no " — ..." suffix).
            self.assertIn("Denied: datadoghq.com (scope=global)", out)
            self.assertNotIn("Denied: datadoghq.com (scope=global) —", out)
            self.assertIn("daemon not reachable", err)
            # finding (PR #85 review, daemon endpoint): the fallback message
            # must name the exact address that was tried, sourced from
            # daemon.json — not a silently-assumed default.
            self.assertIn("daemon not reachable at http://127.0.0.1:1;", err)
            self.assertIn("held requests were NOT swept", err)
            self.assertTrue((egress_root / dl.DENYLIST_FILENAME).exists())

    def test_add_stale_endpoint_file_falls_back_to_direct_write_and_names_url_tried(self):
        """A daemon.json left behind by a crashed daemon (pid no longer
        alive) must be treated as no endpoint at all: read_daemon_endpoint
        returns None, daemon_base_url falls back to the default, and the
        fallback message names THAT address — not the stale pid's port.

        DEFAULT_PORT is patched to 1 (guaranteed refused, same rationale as
        test_add_daemon_unreachable_falls_back_to_direct_write above) so
        this never depends on whether the real default port happens to be
        free on whatever host runs the suite.
        """
        with tempfile.TemporaryDirectory() as tmp:
            base = self._base(tmp)
            egress_root = base / "run" / "egress"
            egress_root.mkdir(parents=True, exist_ok=True)
            (egress_root / "operator.token").write_text("tok\n", encoding="utf-8")
            dead_pid = self._dead_pid()
            (egress_root / broker_host.ENDPOINT_FILENAME).write_text(
                json.dumps(
                    {"version": 1, "host": "127.0.0.1", "port": 9321, "pid": dead_pid}
                ),
                encoding="utf-8",
            )

            with mock.patch.object(broker_host, "DEFAULT_PORT", 1):
                rc, out, err = _run_main(
                    ["add", "stale-endpoint.example", "--global", "--base-path", str(base)]
                )
            self.assertEqual(rc, 0)
            self.assertIn("Denied: stale-endpoint.example (scope=global)", out)
            self.assertIn("daemon not reachable at http://127.0.0.1:1;", err)
            self.assertNotIn("9321", err)
            self.assertIn("held requests were NOT swept", err)
            self.assertTrue((egress_root / dl.DENYLIST_FILENAME).exists())

    def test_add_no_operator_token_falls_back_to_direct_write(self):
        """Daemon never started (no operator.token yet) — same fallback."""
        with tempfile.TemporaryDirectory() as tmp:
            base = self._base(tmp)
            rc, out, err = _run_main(
                ["add", "datadoghq.com", "--global", "--base-path", str(base)]
            )
        self.assertEqual(rc, 0)
        self.assertIn("Denied: datadoghq.com (scope=global)", out)
        self.assertIn("daemon not reachable", err)

    def test_remove_never_talks_to_the_daemon(self):
        """Undeny only widens what's allowed to be asked about again;
        nothing currently held needs releasing — _cmd_remove has no daemon
        path at all (unlike _cmd_add, there is nothing to fall back FROM)."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "home"
            _run_main(["add", "example.com", "--global", "--base-path", str(base)])
            with mock.patch.object(dl, "_try_daemon_deny") as daemon_call:
                rc, _out, _err = _run_main(
                    ["remove", "example.com", "--global", "--base-path", str(base)]
                )
            self.assertEqual(rc, 0)
            daemon_call.assert_not_called()


if __name__ == "__main__":
    unittest.main()
