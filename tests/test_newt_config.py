#!/usr/bin/env python3
"""Unit tests for src/newt_config.py — the singleton Pangolin Newt connector."""

import ipaddress
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
import newt_config  # noqa: E402

SECRETS = {
    "PANGOLIN_ENDPOINT": "https://pangolin.example.com",
    "NEWT_ID": "abcd1234",
    "NEWT_SECRET": "s3cr3t",
}


class IdentityTests(unittest.TestCase):
    def test_deterministic_per_home(self):
        self.assertEqual(
            newt_config.derive_identity(Path("/tmp")),
            newt_config.derive_identity(Path("/tmp")),
        )

    def test_differs_from_the_jump_identity(self):
        # Same DJINN_HOME, different singleton — the container names must not
        # collide on the docker daemon.
        sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
        import jump_config

        n = newt_config.derive_identity(Path("/tmp"))
        j = jump_config.derive_identity(Path("/tmp"))
        self.assertNotEqual(n.container_name, j.container_name)
        self.assertTrue(n.container_name.startswith("djinn-newt-"))


class AddressTests(unittest.TestCase):
    def test_default_is_one_below_the_jump(self):
        import jump_config

        self.assertEqual(newt_config.resolve_newt_ip({}), "172.30.0.253")
        self.assertNotEqual(newt_config.resolve_newt_ip({}), jump_config.resolve_jump_ip({}))

    def test_tracks_a_subnet_override(self):
        self.assertEqual(
            newt_config.resolve_newt_ip({"DJINN_SUBNET": "10.9.0.0/24"}), "10.9.0.253"
        )

    def test_explicit_subnet_argument_wins(self):
        self.assertEqual(
            newt_config.resolve_newt_ip(
                {"DJINN_SUBNET": "10.9.0.0/24"},
                subnet=ipaddress.IPv4Network("172.30.0.0/24"),
            ),
            "172.30.0.253",
        )

    def test_override_validated(self):
        self.assertEqual(
            newt_config.resolve_newt_ip({"DJINN_NEWT_IP": "172.30.0.40"}), "172.30.0.40"
        )
        for bad in ("172.30.0.0", "172.30.0.255", "172.30.0.1", "10.0.0.5", "nope"):
            with self.subTest(bad=bad), self.assertRaises(newt_config.NewtConfigError):
                newt_config.resolve_newt_ip({"DJINN_NEWT_IP": bad})


class ImageTests(unittest.TestCase):
    def test_default_is_pinned(self):
        image = newt_config.resolve_image({})
        self.assertTrue(image.startswith("fosrl/newt:"))
        self.assertNotIn(":latest", image)

    def test_override(self):
        self.assertEqual(
            newt_config.resolve_image({"DJINN_NEWT_IMAGE": "fosrl/newt:1.17.0"}),
            "fosrl/newt:1.17.0",
        )

    def test_untagged_is_rejected(self):
        with self.assertRaises(newt_config.NewtConfigError) as ctx:
            newt_config.resolve_image({"DJINN_NEWT_IMAGE": "fosrl/newt"})
        self.assertIn("no tag", str(ctx.exception))

    def test_latest_is_rejected(self):
        with self.assertRaises(newt_config.NewtConfigError) as ctx:
            newt_config.resolve_image({"DJINN_NEWT_IMAGE": "fosrl/newt:latest"})
        self.assertIn("latest", str(ctx.exception))


class SecretsTests(unittest.TestCase):
    def test_all_present(self):
        self.assertEqual(newt_config.resolve_secrets(dict(SECRETS)), SECRETS)

    def test_missing_are_named_together(self):
        with self.assertRaises(newt_config.NewtConfigError) as ctx:
            newt_config.resolve_secrets({"NEWT_ID": "x"})
        msg = str(ctx.exception)
        self.assertIn("PANGOLIN_ENDPOINT", msg)
        self.assertIn("NEWT_SECRET", msg)
        self.assertNotIn("NEWT_ID,", msg)

    def test_blank_counts_as_missing(self):
        env = dict(SECRETS, NEWT_SECRET="   ")
        with self.assertRaises(newt_config.NewtConfigError):
            newt_config.resolve_secrets(env)


class ComposeRenderTests(unittest.TestCase):
    def _render(self, **kw):
        args = dict(
            identity=newt_config.derive_identity(Path("/tmp")),
            newt_ip="172.30.0.253",
            image="fosrl/newt:1.16.0",
            secrets=dict(SECRETS),
        )
        args.update(kw)
        return newt_config.render_compose_yaml(**args)

    def test_shape(self):
        out = self._render()
        self.assertIn("do not hand-edit", out)
        self.assertIn("image: fosrl/newt:1.16.0", out)
        self.assertIn("ipv4_address: 172.30.0.253", out)
        self.assertIn("name: djinn-net", out)
        self.assertIn("external: true", out)
        for name in newt_config.SECRET_VARS:
            self.assertIn(name, out)

    def test_publishes_no_ports(self):
        # Newt dials OUT — an inbound port would defeat the whole point and
        # put a listener on the Mac.
        self.assertNotIn("ports:", self._render())

    def test_requests_no_capabilities(self):
        # Newt is fully userspace (wireguard-go netstack); NET_ADMIN or a tun
        # device would be an unnecessary privilege grant.
        out = self._render()
        for forbidden in ("cap_add", "NET_ADMIN", "/dev/net/tun", "privileged"):
            self.assertNotIn(forbidden, out)

    def test_secret_values_are_escaped(self):
        out = self._render(
            secrets=dict(SECRETS, NEWT_SECRET='a"b$c\\d')
        )
        self.assertIn(r"\"b", out)
        self.assertIn("$$c", out)

    def test_missing_secret_is_rejected(self):
        with self.assertRaises(newt_config.NewtConfigError):
            self._render(secrets={"NEWT_ID": "x"})

    def test_write_round_trips(self):
        with tempfile.TemporaryDirectory() as home:
            path = newt_config.write_compose_file(Path(home), dict(SECRETS))
            self.assertEqual(path, newt_config.paths(Path(home))["compose_file"])
            self.assertIn("djinn-net", path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
