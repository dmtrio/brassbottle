#!/usr/bin/env python3
"""Unit tests for src/djinn_net_addr.py — the shared djinn-net address rules.

Extracted from jump_config so jump and newt derive addresses identically.
The constant-time property is asserted structurally (hosts() patched to raise)
rather than by timing.
"""

import ipaddress
import sys
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
import djinn_net_addr as na  # noqa: E402


class SubnetTests(unittest.TestCase):
    def test_default(self):
        self.assertEqual(na.resolve_subnet({}), ipaddress.IPv4Network("172.30.0.0/24"))

    def test_blank_falls_back(self):
        self.assertEqual(na.resolve_subnet({"DJINN_SUBNET": "  "}), ipaddress.IPv4Network("172.30.0.0/24"))

    def test_invalid_rejected(self):
        for bad in ("nope", "172.30.0.5/24"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                na.resolve_subnet({"DJINN_SUBNET": bad})


class TopAddressTests(unittest.TestCase):
    NET = ipaddress.IPv4Network("172.30.0.0/24")

    def test_offsets_count_down_from_the_top(self):
        self.assertEqual(str(na.top_address(self.NET, 1)), "172.30.0.254")
        self.assertEqual(str(na.top_address(self.NET, 2)), "172.30.0.253")

    def test_jump_and_newt_never_collide(self):
        self.assertNotEqual(na.top_address(self.NET, 1), na.top_address(self.NET, 2))

    def test_avoids_the_dynamic_allocation_range(self):
        # docker allocates ascending from .2; the top is what keeps a static
        # address from colliding with a running fleet.
        low = {ipaddress.IPv4Address(f"172.30.0.{n}") for n in range(2, 20)}
        self.assertNotIn(na.top_address(self.NET, 1), low)
        self.assertNotIn(na.top_address(self.NET, 2), low)

    def test_tracks_a_subnet_override(self):
        net = ipaddress.IPv4Network("10.9.0.0/24")
        self.assertEqual(str(na.top_address(net, 1)), "10.9.0.254")

    def test_offset_must_be_positive(self):
        with self.assertRaises(ValueError):
            na.top_address(self.NET, 0)

    def test_subnet_too_small_names_the_override(self):
        with self.assertRaises(ValueError) as ctx:
            na.top_address(ipaddress.IPv4Network("10.9.0.0/31"), 1, "DJINN_JUMP_IP")
        self.assertIn("DJINN_JUMP_IP", str(ctx.exception))

    def test_slash_29_is_the_smallest(self):
        self.assertEqual(str(na.top_address(ipaddress.IPv4Network("10.9.0.0/29"), 1)), "10.9.0.6")

    def test_does_not_enumerate_the_subnet(self):
        def boom(self):
            raise AssertionError("must not enumerate the subnet")

        with unittest.mock.patch.object(ipaddress.IPv4Network, "hosts", boom):
            self.assertEqual(
                str(na.top_address(ipaddress.IPv4Network("10.0.0.0/8"), 2)),
                "10.255.255.253",
            )


class ValidateStaticTests(unittest.TestCase):
    NET = ipaddress.IPv4Network("172.30.0.0/24")

    def test_accepts_a_host_address(self):
        self.assertEqual(na.validate_static(self.NET, "172.30.0.50", "X"), "172.30.0.50")

    def test_rejects_outside_subnet(self):
        with self.assertRaises(ValueError):
            na.validate_static(self.NET, "10.0.0.5", "X")

    def test_rejects_network_and_broadcast(self):
        for bad in ("172.30.0.0", "172.30.0.255"):
            with self.subTest(bad=bad), self.assertRaises(ValueError) as ctx:
                na.validate_static(self.NET, bad, "X")
            self.assertIn("assignable", str(ctx.exception))

    def test_rejects_the_gateway(self):
        with self.assertRaises(ValueError) as ctx:
            na.validate_static(self.NET, "172.30.0.1", "X")
        self.assertIn("gateway", str(ctx.exception))

    def test_rejects_garbage(self):
        with self.assertRaises(ValueError):
            na.validate_static(self.NET, "nope", "X")


if __name__ == "__main__":
    unittest.main()
