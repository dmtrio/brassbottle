#!/usr/bin/env python3
"""Unit tests for src/hosts_ipv4.py — dropping the unroutable AAAA
host.docker.internal entry Docker Desktop writes into /etc/hosts.

No real /etc/hosts is touched: rewrite() is pointed at a temp file, which also
lets us assert the in-place (same inode) write the Docker bind mount requires.
"""

import os
import sys
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
import hosts_ipv4

DOCKER_DESKTOP_HOSTS = """\
127.0.0.1\tlocalhost
::1\tlocalhost ip6-localhost ip6-loopback
ff02::2\tip6-allrouters
192.168.65.254\thost.docker.internal
fdc4:f303:9324::254\thost.docker.internal
172.30.0.5\tcoding-brassbottle
"""


class StripIPv6LinesTest(unittest.TestCase):
    def test_drops_the_aaaa_entry_and_keeps_the_a_entry(self):
        out, dropped = hosts_ipv4.strip_ipv6_lines(
            DOCKER_DESKTOP_HOSTS, ["host.docker.internal"]
        )
        self.assertEqual(dropped, ["fdc4:f303:9324::254\thost.docker.internal"])
        self.assertIn("192.168.65.254\thost.docker.internal\n", out)
        self.assertNotIn("fdc4:", out)

    def test_leaves_unrelated_ipv6_lines_alone(self):
        out, _ = hosts_ipv4.strip_ipv6_lines(
            DOCKER_DESKTOP_HOSTS, ["host.docker.internal"]
        )
        self.assertIn("::1\tlocalhost ip6-localhost ip6-loopback\n", out)
        self.assertIn("ff02::2\tip6-allrouters\n", out)

    def test_keeps_the_aaaa_entry_when_no_ipv4_entry_survives(self):
        # Dropping here would make the name unresolvable — worse than slow.
        text = "fdc4:f303:9324::254\thost.docker.internal\n"
        out, dropped = hosts_ipv4.strip_ipv6_lines(text, ["host.docker.internal"])
        self.assertEqual(dropped, [])
        self.assertEqual(out, text)

    def test_is_idempotent(self):
        once, _ = hosts_ipv4.strip_ipv6_lines(
            DOCKER_DESKTOP_HOSTS, ["host.docker.internal"]
        )
        twice, dropped = hosts_ipv4.strip_ipv6_lines(once, ["host.docker.internal"])
        self.assertEqual(dropped, [])
        self.assertEqual(twice, once)

    def test_matches_an_alias_and_ignores_case(self):
        text = (
            "192.168.65.254\tgateway host.docker.internal\n"
            "fdc4::254\tgateway HOST.DOCKER.INTERNAL\n"
        )
        out, changed = hosts_ipv4.strip_ipv6_lines(text, ["host.docker.internal"])
        self.assertEqual(len(changed), 1)
        # Only the target name goes; `gateway` was never asked about and is
        # left exactly as the file had it.
        self.assertNotIn("HOST.DOCKER.INTERNAL", out)
        self.assertIn("fdc4::254\tgateway\n", out)

    def test_an_ipv6_only_alias_sharing_the_line_survives(self):
        # Whole-line removal would take gateway.internal with it, and it has
        # no IPv4 entry of its own — the exact "never make a name
        # unresolvable" guarantee this module claims.
        text = (
            "192.168.65.254\thost.docker.internal\n"
            "fdc4::254\thost.docker.internal gateway.internal\n"
        )
        out, changed = hosts_ipv4.strip_ipv6_lines(text, ["host.docker.internal"])
        self.assertEqual(changed, ["fdc4::254\thost.docker.internal gateway.internal"])
        self.assertIn("fdc4::254\tgateway.internal\n", out)
        self.assertIn("192.168.65.254\thost.docker.internal\n", out)
        self.assertNotIn("::254\thost.docker.internal", out)

    def test_a_line_of_only_target_names_is_dropped_outright(self):
        text = (
            "192.168.65.254\thost.docker.internal\n"
            "fdc4::254\thost.docker.internal\n"
        )
        out, changed = hosts_ipv4.strip_ipv6_lines(text, ["host.docker.internal"])
        self.assertEqual(len(changed), 1)
        self.assertNotIn("fdc4::254", out)

    def test_an_edited_line_keeps_its_trailing_comment(self):
        text = (
            "192.168.65.254\thost.docker.internal\n"
            "fdc4::254 host.docker.internal gateway.internal  # docker desktop\n"
        )
        out, _ = hosts_ipv4.strip_ipv6_lines(text, ["host.docker.internal"])
        self.assertIn("gateway.internal", out)
        self.assertIn("# docker desktop", out)
        self.assertTrue(out.endswith("\n"))

    def test_an_alias_with_no_ipv4_entry_is_never_removed(self):
        # gateway.internal is a target here, but has no surviving A record.
        text = "fdc4::254\thost.docker.internal gateway.internal\n"
        out, changed = hosts_ipv4.strip_ipv6_lines(
            text, ["host.docker.internal", "gateway.internal"]
        )
        self.assertEqual(changed, [])
        self.assertEqual(out, text)

    def test_ignores_comments_and_blank_lines(self):
        text = "# fdc4::254 host.docker.internal\n\n192.168.65.254 host.docker.internal\n"
        out, dropped = hosts_ipv4.strip_ipv6_lines(text, ["host.docker.internal"])
        self.assertEqual(dropped, [])
        self.assertEqual(out, text)

    def test_a_name_not_in_the_list_is_untouched(self):
        out, dropped = hosts_ipv4.strip_ipv6_lines(DOCKER_DESKTOP_HOSTS, ["other.host"])
        self.assertEqual(dropped, [])
        self.assertEqual(out, DOCKER_DESKTOP_HOSTS)


class RewriteTest(unittest.TestCase):
    def setUp(self):
        handle = tempfile.NamedTemporaryFile("w", suffix="-hosts", delete=False)
        handle.write(DOCKER_DESKTOP_HOSTS)
        handle.close()
        self.path = handle.name
        self.addCleanup(os.unlink, self.path)

    def test_rewrites_in_place_keeping_the_same_inode(self):
        # /etc/hosts is a bind mount: a rename would fail or orphan the mount.
        before = os.stat(self.path).st_ino
        dropped = hosts_ipv4.rewrite(path=self.path, names=["host.docker.internal"])
        self.assertEqual(len(dropped), 1)
        self.assertEqual(os.stat(self.path).st_ino, before)
        self.assertNotIn("fdc4:", Path(self.path).read_text())

    def test_does_not_write_when_nothing_is_dropped(self):
        hosts_ipv4.rewrite(path=self.path, names=["host.docker.internal"])
        after_first = os.stat(self.path).st_mtime_ns
        self.assertEqual(
            hosts_ipv4.rewrite(path=self.path, names=["host.docker.internal"]), []
        )
        self.assertEqual(os.stat(self.path).st_mtime_ns, after_first)


class MainTest(unittest.TestCase):
    def test_reports_each_dropped_line_and_exits_zero(self):
        with mock.patch.object(hosts_ipv4, "rewrite", return_value=["fdc4::254 h"]):
            with mock.patch("sys.stdout", new=StringIO()) as out:
                self.assertEqual(hosts_ipv4.main([]), 0)
        self.assertIn("dropped unroutable AAAA entry", out.getvalue())

    def test_an_unwritable_hosts_file_warns_and_is_non_fatal(self):
        with mock.patch.object(hosts_ipv4, "rewrite", side_effect=PermissionError("ro")):
            with mock.patch("sys.stdout", new=StringIO()) as out:
                self.assertEqual(hosts_ipv4.main([]), 1)
        self.assertIn("could not drop AAAA entries", out.getvalue())

    def test_argv_overrides_the_default_name(self):
        with mock.patch.object(hosts_ipv4, "rewrite", return_value=[]) as rewrite:
            hosts_ipv4.main(["gateway.example"])
        self.assertEqual(rewrite.call_args.kwargs["names"], ("gateway.example",))


if __name__ == "__main__":
    unittest.main()
