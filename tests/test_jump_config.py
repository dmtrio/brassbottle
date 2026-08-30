#!/usr/bin/env python3
"""Unit tests for src/jump_config.py — the singleton jump container's paths,
identity, address resolution and compose rendering.

No docker, no filesystem outside a temp dir. The address tests matter most:
resolve_jump_ip() derives from DJINN_SUBNET, so a subnet override must not
silently leave the jump on an address outside its own bridge.
"""

import ipaddress
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
import jump_config  # noqa: E402


class IdentityTests(unittest.TestCase):
    def test_identity_is_deterministic_per_home(self):
        a = jump_config.derive_identity(Path("/tmp"))
        b = jump_config.derive_identity(Path("/tmp"))
        self.assertEqual(a, b)

    def test_identity_differs_between_homes(self):
        with tempfile.TemporaryDirectory() as one, tempfile.TemporaryDirectory() as two:
            self.assertNotEqual(
                jump_config.derive_identity(Path(one)),
                jump_config.derive_identity(Path(two)),
            )

    def test_identity_names_are_prefixed_and_suffixed(self):
        ident = jump_config.derive_identity(Path("/tmp"))
        self.assertTrue(ident.container_name.startswith("djinn-jump-"))
        self.assertEqual(len(ident.suffix), jump_config.IDENTITY_SUFFIX_LENGTH)
        self.assertEqual(ident.image_tag, f"djinn-jump:{ident.suffix}")

    def test_identity_never_embeds_the_home_path(self):
        with tempfile.TemporaryDirectory() as home:
            ident = jump_config.derive_identity(Path(home))
            self.assertNotIn(Path(home).name, ident.container_name)


class SubnetTests(unittest.TestCase):
    def test_default_subnet_when_unset(self):
        self.assertEqual(
            jump_config.resolve_subnet({}),
            ipaddress.IPv4Network(jump_config.DEFAULT_SUBNET),
        )

    def test_blank_subnet_falls_back_to_default(self):
        self.assertEqual(
            jump_config.resolve_subnet({"DJINN_SUBNET": "   "}),
            ipaddress.IPv4Network(jump_config.DEFAULT_SUBNET),
        )

    def test_invalid_subnet_is_rejected(self):
        with self.assertRaises(jump_config.JumpConfigError):
            jump_config.resolve_subnet({"DJINN_SUBNET": "not-a-cidr"})

    def test_host_bits_set_is_rejected(self):
        with self.assertRaises(jump_config.JumpConfigError):
            jump_config.resolve_subnet({"DJINN_SUBNET": "172.30.0.5/24"})


class JumpIpTests(unittest.TestCase):
    def test_default_is_last_usable_address(self):
        # NOT .2: docker's dynamic pool allocates ascending from .2, so the low
        # addresses are exactly where bottles land and a static .2 collides on
        # any installation that already has one.
        self.assertEqual(jump_config.resolve_jump_ip({}), "172.30.0.254")

    def test_default_avoids_the_dynamic_allocation_range(self):
        ip = jump_config.resolve_jump_ip({})
        self.assertNotIn(ip, {"172.30.0.2", "172.30.0.3", "172.30.0.4"})

    def test_default_is_not_the_broadcast_address(self):
        self.assertNotEqual(jump_config.resolve_jump_ip({}), "172.30.0.255")

    def test_tracks_a_subnet_override(self):
        # The whole point: a DJINN_SUBNET override must move the jump with it.
        self.assertEqual(
            jump_config.resolve_jump_ip({"DJINN_SUBNET": "10.9.0.0/24"}), "10.9.0.254"
        )

    def test_subnet_too_small_is_rejected(self):
        with self.assertRaises(jump_config.JumpConfigError) as ctx:
            jump_config.resolve_jump_ip({"DJINN_SUBNET": "10.9.0.0/31"})
        self.assertIn("DJINN_JUMP_IP", str(ctx.exception))

    def test_slash_29_is_the_smallest_derivable(self):
        self.assertEqual(
            jump_config.resolve_jump_ip({"DJINN_SUBNET": "10.9.0.0/29"}), "10.9.0.6"
        )

    def test_explicit_override_wins(self):
        self.assertEqual(
            jump_config.resolve_jump_ip({"DJINN_JUMP_IP": "172.30.0.50"}), "172.30.0.50"
        )

    def test_override_outside_subnet_is_rejected(self):
        with self.assertRaises(jump_config.JumpConfigError) as ctx:
            jump_config.resolve_jump_ip({"DJINN_JUMP_IP": "10.0.0.5"})
        self.assertIn("assignable", str(ctx.exception))

    def test_override_on_network_or_broadcast_is_rejected(self):
        # Both are inside the subnet but not assignable; docker rejects them
        # with an opaque IPAM error far downstream of the typo.
        for bad in ("172.30.0.0", "172.30.0.255"):
            with self.subTest(bad=bad):
                with self.assertRaises(jump_config.JumpConfigError) as ctx:
                    jump_config.resolve_jump_ip({"DJINN_JUMP_IP": bad})
                self.assertIn("assignable", str(ctx.exception))

    def test_override_on_the_gateway_is_rejected(self):
        with self.assertRaises(jump_config.JumpConfigError) as ctx:
            jump_config.resolve_jump_ip({"DJINN_JUMP_IP": "172.30.0.1"})
        self.assertIn("gateway", str(ctx.exception))

    def test_invalid_override_is_rejected(self):
        with self.assertRaises(jump_config.JumpConfigError):
            jump_config.resolve_jump_ip({"DJINN_JUMP_IP": "nope"})


class MoshPortsTests(unittest.TestCase):
    def test_default(self):
        self.assertEqual(jump_config.resolve_mosh_ports({}), jump_config.DEFAULT_MOSH_PORTS)

    def test_valid_override(self):
        self.assertEqual(
            jump_config.resolve_mosh_ports({"DJINN_JUMP_MOSH_PORTS": "61000:61010"}),
            "61000:61010",
        )

    def test_bad_shape_is_rejected(self):
        for bad in ("60000", "60000-60010", "a:b", ""):
            with self.subTest(bad=bad):
                env = {"DJINN_JUMP_MOSH_PORTS": bad}
                if bad == "":
                    self.assertEqual(
                        jump_config.resolve_mosh_ports(env), jump_config.DEFAULT_MOSH_PORTS
                    )
                else:
                    with self.assertRaises(jump_config.JumpConfigError):
                        jump_config.resolve_mosh_ports(env)

    def test_reversed_range_is_rejected(self):
        with self.assertRaises(jump_config.JumpConfigError) as ctx:
            jump_config.resolve_mosh_ports({"DJINN_JUMP_MOSH_PORTS": "60010:60000"})
        self.assertIn("START", str(ctx.exception))

    def test_out_of_range_is_rejected(self):
        with self.assertRaises(jump_config.JumpConfigError):
            jump_config.resolve_mosh_ports({"DJINN_JUMP_MOSH_PORTS": "0:70000"})


class LayoutTests(unittest.TestCase):
    def test_ensure_layout_creates_dirs_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as home:
            base = Path(home)
            for _ in range(2):
                p = jump_config.ensure_layout(base)
                self.assertTrue(p["ssh_dir"].is_dir())
                self.assertTrue(p["compose_dir"].is_dir())

    def test_ensure_layout_does_not_create_key_material(self):
        # Keys are generated in the container's entrypoint; the host must not
        # need to hold or create them.
        with tempfile.TemporaryDirectory() as home:
            p = jump_config.ensure_layout(Path(home))
            self.assertFalse(p["client_key"].exists())
            self.assertFalse(p["client_pubkey"].exists())


class ComposeRenderTests(unittest.TestCase):
    KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5 operator@mac"

    def _render(self, home, **kw):
        p = jump_config.ensure_layout(Path(home))
        args = dict(
            identity=jump_config.derive_identity(Path(home)),
            ssh_dir=p["ssh_dir"],
            jump_ip="172.30.0.254",
            mosh_ports="60000:60010",
            authorized_key=self.KEY,
        )
        args.update(kw)
        return jump_config.render_compose_yaml(**args)

    def test_renders_expected_shape(self):
        with tempfile.TemporaryDirectory() as home:
            out = self._render(home)
            self.assertIn("do not hand-edit", out)
            self.assertIn("dockerfile: jump/Dockerfile", out)
            self.assertIn("ipv4_address: 172.30.0.254", out)
            self.assertIn("name: djinn-net", out)
            self.assertIn("external: true", out)
            self.assertIn(self.KEY, out)
            self.assertIn('MOSH_PORTS: "60000:60010"', out)

    def test_publishes_no_host_ports(self):
        # Load-bearing: the jump is reached at its bridge IP over the tunnel.
        # A ports: block would reintroduce the host-port exclusivity that
        # forced per-bottle mosh ranges, and would expose it beyond the tunnel.
        with tempfile.TemporaryDirectory() as home:
            self.assertNotIn("ports:", self._render(home))

    def test_empty_key_is_rejected(self):
        with tempfile.TemporaryDirectory() as home:
            with self.assertRaises(jump_config.JumpConfigError) as ctx:
                self._render(home, authorized_key="   ")
            self.assertIn("SSH_AUTHORIZED_KEY", str(ctx.exception))

    def test_key_with_quotes_is_escaped_not_interpolated(self):
        # A `"` or `\\` in the key's comment would break the YAML scalar, and a
        # `$` would be eaten by docker compose's ${VAR} interpolation.
        with tempfile.TemporaryDirectory() as home:
            out = self._render(home, authorized_key='ssh-ed25519 AAAA me "work$laptop"')
            self.assertNotIn('SSH_AUTHORIZED_KEY: "ssh-ed25519 AAAA me "work', out)
            self.assertIn(r'\"work', out)
            self.assertIn("$$laptop", out)

    def test_multiline_key_is_rejected(self):
        # A newline would break out of the quoted YAML scalar.
        with tempfile.TemporaryDirectory() as home:
            with self.assertRaises(jump_config.JumpConfigError):
                self._render(home, authorized_key=f"{self.KEY}\nssh-ed25519 BBBB x")

    def test_missing_ssh_dir_is_rejected(self):
        with tempfile.TemporaryDirectory() as home:
            with self.assertRaises(jump_config.JumpConfigError):
                jump_config.render_compose_yaml(
                    identity=jump_config.derive_identity(Path(home)),
                    ssh_dir=Path(home) / "absent",
                    jump_ip="172.30.0.2",
                    mosh_ports="60000:60010",
                    authorized_key=self.KEY,
                )

    def test_write_compose_file_round_trips(self):
        with tempfile.TemporaryDirectory() as home:
            path = jump_config.write_compose_file(Path(home), self.KEY)
            self.assertEqual(path, jump_config.paths(Path(home))["compose_file"])
            self.assertIn("djinn-net", path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
