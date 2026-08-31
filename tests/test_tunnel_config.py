#!/usr/bin/env python3
"""Unit tests for src/tunnel_config.py — the singleton Pangolin Newt connector."""

import ipaddress
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
import tunnel_config  # noqa: E402

SECRETS = {
    "PANGOLIN_ENDPOINT": "https://pangolin.example.com",
    "NEWT_ID": "abcd1234",
    "NEWT_SECRET": "s3cr3t",
}


class VendorNeutralityTests(unittest.TestCase):
    """The CLI surface and the operator-facing names must not name a vendor.

    `main` had zero product names before this feature, deliberately: RFC 04's
    PR notes record the docs being reworded to say "WireGuard/VPN" generically.
    The provider belongs in one marked block, not in the command you type.
    """

    ROOT = Path(__file__).parent.parent

    def test_the_provider_is_confined_to_one_named_constant(self):
        self.assertEqual(tunnel_config.PROVIDER, "newt")
        self.assertIn(tunnel_config.PROVIDER, tunnel_config.DEFAULT_TUNNEL_IMAGE)

    VENDORS = ("newt", "pangolin", "olm", "tailscale")

    def test_the_dispatcher_names_no_vendor(self):
        text = (self.ROOT / "djinn").read_text(encoding="utf-8").lower()
        for vendor in self.VENDORS:
            with self.subTest(vendor=vendor):
                self.assertNotIn(vendor, text)

    def test_tunnel_sh_names_vendors_only_via_the_credential_vars(self):
        # It has to export the provider's own variable names; everything else
        # in it — including the prose — stays role-shaped.
        text = (self.ROOT / "tunnel.sh").read_text(encoding="utf-8")
        stripped = text
        for var in tunnel_config.SECRET_VARS:
            stripped = stripped.replace(var, "")
        for vendor in self.VENDORS:
            with self.subTest(vendor=vendor):
                self.assertNotIn(vendor, stripped.lower())

    def test_our_env_vars_name_the_role_not_the_vendor(self):
        self.assertEqual(tunnel_config.ENV_TUNNEL_IP, "DJINN_TUNNEL_IP")
        self.assertEqual(tunnel_config.ENV_TUNNEL_IMAGE, "DJINN_TUNNEL_IMAGE")

    def test_provider_credentials_keep_their_upstream_names(self):
        # These must match what the provider's admin UI shows, so they are the
        # one place a vendor name is correct — and they live in secrets.env.
        self.assertEqual(
            tunnel_config.SECRET_VARS,
            ("PANGOLIN_ENDPOINT", "NEWT_ID", "NEWT_SECRET"),
        )


class IdentityTests(unittest.TestCase):
    def test_deterministic_per_home(self):
        self.assertEqual(
            tunnel_config.derive_identity(Path("/tmp")),
            tunnel_config.derive_identity(Path("/tmp")),
        )

    def test_differs_from_the_jump_identity(self):
        # Same DJINN_HOME, different singleton — the container names must not
        # collide on the docker daemon.
        sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
        import jump_config

        n = tunnel_config.derive_identity(Path("/tmp"))
        j = jump_config.derive_identity(Path("/tmp"))
        self.assertNotEqual(n.container_name, j.container_name)
        self.assertTrue(n.container_name.startswith("djinn-tunnel-"))


class AddressTests(unittest.TestCase):
    def test_default_is_one_below_the_jump(self):
        import jump_config

        self.assertEqual(tunnel_config.resolve_tunnel_ip({}), "172.30.0.253")
        self.assertNotEqual(tunnel_config.resolve_tunnel_ip({}), jump_config.resolve_jump_ip({}))

    def test_tracks_a_subnet_override(self):
        self.assertEqual(
            tunnel_config.resolve_tunnel_ip({"DJINN_SUBNET": "10.9.0.0/24"}), "10.9.0.253"
        )

    def test_explicit_subnet_argument_wins(self):
        self.assertEqual(
            tunnel_config.resolve_tunnel_ip(
                {"DJINN_SUBNET": "10.9.0.0/24"},
                subnet=ipaddress.IPv4Network("172.30.0.0/24"),
            ),
            "172.30.0.253",
        )

    def test_override_validated(self):
        self.assertEqual(
            tunnel_config.resolve_tunnel_ip({"DJINN_TUNNEL_IP": "172.30.0.40"}), "172.30.0.40"
        )
        for bad in ("172.30.0.0", "172.30.0.255", "172.30.0.1", "10.0.0.5", "nope"):
            with self.subTest(bad=bad), self.assertRaises(tunnel_config.TunnelConfigError):
                tunnel_config.resolve_tunnel_ip({"DJINN_TUNNEL_IP": bad})


class ImageTests(unittest.TestCase):
    def test_default_is_pinned(self):
        image = tunnel_config.resolve_image({})
        self.assertTrue(image.startswith("fosrl/newt:"))
        self.assertNotIn(":latest", image)

    def test_default_is_the_multiarch_tag_not_a_per_arch_one(self):
        # fosrl publishes arm64-/amd64-/armv7- prefixed tags alongside the
        # multi-arch manifest; pinning one would break on another host.
        tag = tunnel_config.DEFAULT_TUNNEL_IMAGE.rsplit(":", 1)[-1]
        for arch in ("arm64-", "amd64-", "armv7-"):
            self.assertFalse(tag.startswith(arch))

    def test_override(self):
        self.assertEqual(
            tunnel_config.resolve_image({"DJINN_TUNNEL_IMAGE": "fosrl/newt:1.17.0"}),
            "fosrl/newt:1.17.0",
        )

    def test_untagged_is_rejected(self):
        with self.assertRaises(tunnel_config.TunnelConfigError) as ctx:
            tunnel_config.resolve_image({"DJINN_TUNNEL_IMAGE": "fosrl/newt"})
        self.assertIn("no tag", str(ctx.exception))

    def test_latest_is_rejected(self):
        with self.assertRaises(tunnel_config.TunnelConfigError) as ctx:
            tunnel_config.resolve_image({"DJINN_TUNNEL_IMAGE": "fosrl/newt:latest"})
        self.assertIn("latest", str(ctx.exception))


class SecretsTests(unittest.TestCase):
    def test_all_present(self):
        self.assertEqual(tunnel_config.resolve_secrets(dict(SECRETS)), SECRETS)

    def test_missing_are_named_together(self):
        with self.assertRaises(tunnel_config.TunnelConfigError) as ctx:
            tunnel_config.resolve_secrets({"NEWT_ID": "x"})
        msg = str(ctx.exception)
        self.assertIn("PANGOLIN_ENDPOINT", msg)
        self.assertIn("NEWT_SECRET", msg)
        self.assertNotIn("NEWT_ID,", msg)

    def test_blank_counts_as_missing(self):
        env = dict(SECRETS, NEWT_SECRET="   ")
        with self.assertRaises(tunnel_config.TunnelConfigError):
            tunnel_config.resolve_secrets(env)


class EnvFileTests(unittest.TestCase):
    def test_written_0600_with_every_secret(self):
        with tempfile.TemporaryDirectory() as home:
            path = tunnel_config.write_env_file(Path(home), dict(SECRETS))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            body = path.read_text(encoding="utf-8")
            for name, value in SECRETS.items():
                self.assertIn(f"{name}={value}", body)

    def test_rewrite_is_idempotent_and_keeps_the_mode(self):
        with tempfile.TemporaryDirectory() as home:
            tunnel_config.write_env_file(Path(home), dict(SECRETS))
            path = tunnel_config.write_env_file(Path(home), dict(SECRETS, NEWT_ID="other"))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertIn("NEWT_ID=other", path.read_text(encoding="utf-8"))

    def test_no_stale_tmp_file_left_behind(self):
        with tempfile.TemporaryDirectory() as home:
            tunnel_config.write_env_file(Path(home), dict(SECRETS))
            leftovers = list((Path(home) / "compose").glob("*.tmp"))
            self.assertEqual(leftovers, [])

    def test_remove_env_file_is_safe_when_absent(self):
        with tempfile.TemporaryDirectory() as home:
            tunnel_config.remove_env_file(Path(home))  # must not raise
            tunnel_config.write_env_file(Path(home), dict(SECRETS))
            tunnel_config.remove_env_file(Path(home))
            self.assertFalse(tunnel_config.paths(Path(home))["env_file"].exists())


class ComposeRenderTests(unittest.TestCase):
    def _render(self, **kw):
        args = dict(
            identity=tunnel_config.derive_identity(Path("/tmp")),
            tunnel_ip="172.30.0.253",
            image="fosrl/newt:1.16.0",
            env_file=Path("/tmp/djinn/compose/tunnel.env"),
        )
        args.update(kw)
        return tunnel_config.render_compose_yaml(**args)

    def test_shape(self):
        out = self._render()
        self.assertIn("do not hand-edit", out)
        self.assertIn("image: fosrl/newt:1.16.0", out)
        self.assertIn("ipv4_address: 172.30.0.253", out)
        self.assertIn("name: djinn-net", out)
        self.assertIn("env_file:", out)

    def test_compose_carries_no_secret(self):
        # The whole point of the env_file split: the overlay is written at the
        # process umask, so it must not contain a credential.
        out = self._render()
        for value in SECRETS.values():
            self.assertNotIn(value, out)
        self.assertNotIn("NEWT_SECRET", out)

    def test_publishes_no_ports(self):
        self.assertNotIn("ports:", self._render())

    def test_requests_no_capabilities(self):
        out = self._render()
        for forbidden in ("cap_add", "NET_ADMIN", "/dev/net/tun", "privileged"):
            self.assertNotIn(forbidden, out)

    def test_env_file_path_is_escaped(self):
        out = self._render(env_file=Path('/tmp/dj inn #home/compose/tunnel.env'))
        self.assertIn('"', out.split("env_file:")[1].split("\n")[1])
        self.assertIn("#home", out)

    def test_write_round_trips_and_splits_the_secret_out(self):
        with tempfile.TemporaryDirectory() as home:
            path = tunnel_config.write_compose_file(Path(home), dict(SECRETS))
            compose = path.read_text(encoding="utf-8")
            self.assertIn("djinn-net", compose)
            self.assertNotIn("s3cr3t", compose)
            env_file = tunnel_config.paths(Path(home))["env_file"]
            self.assertEqual(env_file.stat().st_mode & 0o777, 0o600)
            self.assertIn("s3cr3t", env_file.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
