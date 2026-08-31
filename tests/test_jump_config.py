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
import unittest.mock
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

    def test_large_subnet_does_not_enumerate_hosts(self):
        # DJINN_SUBNET permits large networks; a /8 would materialise ~16.7M
        # IPv4Address objects if this enumerated. Patching hosts() to raise
        # pins the constant-time path without a flaky timing assertion.
        import ipaddress as _ip

        def boom(self):
            raise AssertionError("resolve_jump_ip must not enumerate the subnet")

        with unittest.mock.patch.object(_ip.IPv4Network, "hosts", boom):
            self.assertEqual(
                jump_config.resolve_jump_ip({"DJINN_SUBNET": "10.0.0.0/8"}),
                "10.255.255.254",
            )
            with self.assertRaises(jump_config.JumpConfigError):
                jump_config.resolve_jump_ip(
                    {"DJINN_SUBNET": "10.0.0.0/8", "DJINN_JUMP_IP": "10.255.255.255"}
                )

    def test_slash_29_is_the_smallest_derivable(self):
        self.assertEqual(
            jump_config.resolve_jump_ip({"DJINN_SUBNET": "10.9.0.0/29"}), "10.9.0.6"
        )

    def test_explicit_subnet_argument_overrides_the_env(self):
        # cmd_start passes the LIVE bridge subnet; it must win over
        # DJINN_SUBNET, which ensure_net only warns about on a mismatch.
        import ipaddress as _ip

        self.assertEqual(
            jump_config.resolve_jump_ip(
                {"DJINN_SUBNET": "10.9.0.0/24"},
                subnet=_ip.IPv4Network("172.30.0.0/24"),
            ),
            "172.30.0.254",
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


class AuthorizedKeysTests(unittest.TestCase):
    KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5 operator@mac"
    KEY2 = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE6 phone@moshi"

    def test_parses_multiple_keys(self):
        keys = jump_config.parse_authorized_keys(
            f"{self.KEY}\n{self.KEY2}\n", source="t"
        )
        self.assertEqual(keys, [self.KEY, self.KEY2])

    def test_skips_blanks_and_comments(self):
        text = f"# my mac\n{self.KEY}\n\n   \n# phone\n{self.KEY2}\n"
        self.assertEqual(
            jump_config.parse_authorized_keys(text, source="t"),
            [self.KEY, self.KEY2],
        )

    def test_strips_carriage_returns(self):
        # A file edited on Windows, or a key pasted through a phone, would
        # otherwise leave a CR that sshd treats as part of the key.
        keys = jump_config.parse_authorized_keys(
            f"{self.KEY}\r\n{self.KEY2}\r\n", source="t"
        )
        self.assertEqual(keys, [self.KEY, self.KEY2])

    def test_accepts_openssh_options(self):
        # An already-hardened key list must survive migration: docs promise
        # standard authorized_keys input, and `restrict` / `from=` are that.
        lines = [
            "restrict,port-forwarding=no " + self.KEY,
            'from="10.0.0.0/8" ' + self.KEY2,
        ]
        self.assertEqual(
            jump_config.parse_authorized_keys("\n".join(lines), source="t"), lines
        )

    def test_options_are_passed_through_untouched(self):
        # sshd is the authority on what options mean; this parser must not
        # normalise or drop them, since the file is copied verbatim.
        line = 'command="/bin/false",no-pty ' + self.KEY
        self.assertEqual(
            jump_config.parse_authorized_keys(line, source="t"), [line]
        )

    def test_quoted_option_value_is_not_mistaken_for_the_key(self):
        # `command="ssh-ed25519 decoy"` contains a token that looks exactly
        # like a key type. A plain .split() would validate the decoy and let
        # a line through whose REAL key type was never checked.
        line = 'command="ssh-ed25519 decoy",restrict ssh-ed25519 AAAAREAL phone'
        self.assertEqual(
            jump_config.parse_authorized_keys(line, source="t"), [line]
        )
        opts, rest = jump_config._split_leading_options(line)
        self.assertEqual(rest, "ssh-ed25519 AAAAREAL phone")

    def test_bad_key_type_after_options_is_still_rejected(self):
        with self.assertRaises(jump_config.JumpConfigError) as ctx:
            jump_config.parse_authorized_keys("restrict nope AAAA x", source="t")
        self.assertIn("after options", str(ctx.exception))

    def test_options_with_no_key_material_is_rejected(self):
        with self.assertRaises(jump_config.JumpConfigError):
            jump_config.parse_authorized_keys("restrict ssh-ed25519", source="t")

    def test_unknown_key_type_names_the_line(self):
        with self.assertRaises(jump_config.JumpConfigError) as ctx:
            jump_config.parse_authorized_keys(
                f"{self.KEY}\nnope AAAA x\n", source="keys"
            )
        self.assertIn("line 2", str(ctx.exception))
        self.assertIn("nope", str(ctx.exception))

    def test_key_type_with_no_material_is_rejected(self):
        with self.assertRaises(jump_config.JumpConfigError):
            jump_config.parse_authorized_keys("ssh-ed25519\n", source="t")

    def test_file_wins_over_env_seed(self):
        # Precedence is one-directional: once the file has a key, secrets.env
        # stops mattering, or an operator could never remove a key the env
        # keeps re-adding.
        with tempfile.TemporaryDirectory() as home:
            jump_config.ensure_layout(Path(home))
            jump_config.seed_authorized_keys(Path(home), [self.KEY2])
            keys, source = jump_config.resolve_authorized_keys(
                Path(home), {"SSH_AUTHORIZED_KEY": self.KEY}
            )
            self.assertEqual((keys, source), ([self.KEY2], "file"))

    def test_env_seeds_when_file_absent(self):
        with tempfile.TemporaryDirectory() as home:
            keys, source = jump_config.resolve_authorized_keys(
                Path(home), {"SSH_AUTHORIZED_KEY": self.KEY}
            )
            self.assertEqual((keys, source), ([self.KEY], "env"))

    def test_no_keys_anywhere_names_both_sources(self):
        with tempfile.TemporaryDirectory() as home:
            with self.assertRaises(jump_config.JumpConfigError) as ctx:
                jump_config.resolve_authorized_keys(Path(home), {})
            msg = str(ctx.exception)
            self.assertIn("authorized_keys", msg)
            self.assertIn("SSH_AUTHORIZED_KEY", msg)

    def test_emptying_the_file_never_restores_the_env_key(self):
        # Revocation safety. Clearing the file is the obvious way to revoke
        # every device; falling back to SSH_AUTHORIZED_KEY there would
        # silently RE-AUTHORISE the key the operator just removed.
        with tempfile.TemporaryDirectory() as home:
            jump_config.ensure_layout(Path(home))
            jump_config.paths(Path(home))["authorized_keys"].write_text("# nothing\n")
            with self.assertRaises(jump_config.JumpConfigError) as ctx:
                jump_config.resolve_authorized_keys(
                    Path(home), {"SSH_AUTHORIZED_KEY": self.KEY}
                )
            msg = str(ctx.exception)
            self.assertIn("no keys", msg)
            # The way back is named, so the error is not a dead end.
            self.assertIn("delete the file", msg)

    def test_read_distinguishes_absent_from_empty(self):
        with tempfile.TemporaryDirectory() as home:
            self.assertIsNone(jump_config.read_authorized_keys(Path(home)))
            jump_config.ensure_layout(Path(home))
            jump_config.paths(Path(home))["authorized_keys"].write_text("")
            self.assertEqual(jump_config.read_authorized_keys(Path(home)), [])

    def test_operator_file_is_never_rewritten(self):
        # docs/remote.md advertises `# mac` / `# phone` labels, and those are
        # the only thing telling the operator which key to revoke later.
        # Rewriting the parsed keys on every start would silently strip them.
        original = f"# mac\n{self.KEY}\n\n# phone\n{self.KEY2}\n"
        with tempfile.TemporaryDirectory() as home:
            jump_config.ensure_layout(Path(home))
            path = jump_config.paths(Path(home))["authorized_keys"]
            path.write_text(original)
            keys, source = jump_config.resolve_authorized_keys(Path(home), {})
            self.assertEqual(source, "file")
            jump_config.write_compose_file(Path(home), keys, seed=(source == "env"))
            self.assertEqual(path.read_text(), original)

    def test_digest_tracks_the_set_not_the_order(self):
        # Reordering the file must not recreate the container; adding or
        # removing a key must.
        a = jump_config.authorized_keys_digest([self.KEY, self.KEY2])
        self.assertEqual(a, jump_config.authorized_keys_digest([self.KEY2, self.KEY]))
        self.assertNotEqual(a, jump_config.authorized_keys_digest([self.KEY]))

    def test_write_authorized_keys_is_readable_and_one_per_line(self):
        with tempfile.TemporaryDirectory() as home:
            path = jump_config.seed_authorized_keys(Path(home), [self.KEY, self.KEY2])
            self.assertEqual(
                path.read_text(encoding="utf-8"), f"{self.KEY}\n{self.KEY2}\n"
            )
            self.assertEqual(path.stat().st_mode & 0o777, 0o644)


class ComposeRenderTests(unittest.TestCase):
    KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5 operator@mac"
    KEY2 = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE6 phone@moshi"

    def _render(self, home, keys=None, **kw):
        p = jump_config.ensure_layout(Path(home))
        keys = keys or [self.KEY]
        keys_file = jump_config.seed_authorized_keys(Path(home), keys)
        args = dict(
            identity=jump_config.derive_identity(Path(home)),
            ssh_dir=p["ssh_dir"],
            authorized_keys_file=keys_file,
            keys_digest=jump_config.authorized_keys_digest(keys),
            jump_ip="172.30.0.254",
            mosh_ports="60000:60010",
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
            self.assertIn('MOSH_PORTS: "60000:60010"', out)

    def test_key_material_is_mounted_never_embedded(self):
        # The whole point of the file: no key content in the compose overlay,
        # so multiple keys need no YAML scalar carrying newlines.
        with tempfile.TemporaryDirectory() as home:
            out = self._render(home, keys=[self.KEY, self.KEY2])
            self.assertNotIn(self.KEY, out)
            self.assertNotIn("SSH_AUTHORIZED_KEY", out)
            self.assertIn(jump_config.AUTHORIZED_KEYS_MOUNT, out)

    def test_key_change_changes_the_overlay(self):
        # Without this the overlay is byte-identical after an operator adds a
        # key (the list is mounted, not embedded), so `compose up -d` sees an
        # unchanged config, leaves the old container running, and reports
        # success while the new device still cannot log in.
        with tempfile.TemporaryDirectory() as home:
            one = self._render(home, keys=[self.KEY])
            two = self._render(home, keys=[self.KEY, self.KEY2])
            self.assertNotEqual(one, two)
            self.assertIn("djinn.authorized_keys_sha256", one)

    def test_reordering_keys_does_not_change_the_overlay(self):
        # A pointless recreate on every start would drop live mosh sessions.
        with tempfile.TemporaryDirectory() as home:
            a = self._render(home, keys=[self.KEY, self.KEY2])
            b = self._render(home, keys=[self.KEY2, self.KEY])
            self.assertEqual(a, b)

    def test_keys_mount_is_read_only(self):
        # A compromised jump must not be able to authorise a new device.
        with tempfile.TemporaryDirectory() as home:
            out = self._render(home)
            line = [
                ln for ln in out.splitlines()
                if jump_config.AUTHORIZED_KEYS_MOUNT in ln
            ][0]
            self.assertTrue(line.rstrip().endswith(':ro"'), line)

    def test_volume_path_is_escaped(self):
        # A DJINN_HOME containing " #" would truncate the scalar at a YAML
        # comment and silently mount a shorter path — the jump would then
        # regenerate its keys somewhere the operator never sees.
        with tempfile.TemporaryDirectory() as home:
            odd = Path(home) / "dj inn #home"
            (odd / "jump" / "ssh").mkdir(parents=True)
            keys_file = jump_config.seed_authorized_keys(odd, [self.KEY])
            out = jump_config.render_compose_yaml(
                identity=jump_config.derive_identity(odd),
                ssh_dir=odd / "jump" / "ssh",
                authorized_keys_file=keys_file,
                keys_digest=jump_config.authorized_keys_digest([self.KEY]),
                jump_ip="172.30.0.254",
                mosh_ports="60000:60010",
            )
            self.assertIn('"', out.split("volumes:")[1].split("\n")[1])
            self.assertIn("#home", out)

    def test_publishes_no_host_ports(self):
        # Load-bearing: the jump is reached at its bridge IP over the tunnel.
        # A ports: block would reintroduce the host-port exclusivity that
        # forced per-bottle mosh ranges, and would expose it beyond the tunnel.
        with tempfile.TemporaryDirectory() as home:
            self.assertNotIn("ports:", self._render(home))

    def test_missing_keys_file_is_rejected(self):
        # Docker creates a DIRECTORY at a missing bind-mount source, so a
        # hand-deleted file must fail here, not become a silent crash loop.
        with tempfile.TemporaryDirectory() as home:
            p = jump_config.ensure_layout(Path(home))
            with self.assertRaises(jump_config.JumpConfigError) as ctx:
                self._render(home, authorized_keys_file=p["jump_root"] / "absent")
            self.assertIn("authorized_keys", str(ctx.exception))

    def test_missing_ssh_dir_is_rejected(self):
        with tempfile.TemporaryDirectory() as home:
            keys_file = jump_config.seed_authorized_keys(Path(home), [self.KEY])
            with self.assertRaises(jump_config.JumpConfigError):
                jump_config.render_compose_yaml(
                    identity=jump_config.derive_identity(Path(home)),
                    ssh_dir=Path(home) / "absent",
                    authorized_keys_file=keys_file,
                    keys_digest=jump_config.authorized_keys_digest([self.KEY]),
                    jump_ip="172.30.0.2",
                    mosh_ports="60000:60010",
                )

    def test_write_compose_file_rejects_a_bare_string(self):
        # The pre-file signature took one key as a str; passing one now would
        # sort its characters into the digest and write it one char per line.
        with tempfile.TemporaryDirectory() as home:
            with self.assertRaises(jump_config.JumpConfigError):
                jump_config.write_compose_file(Path(home), self.KEY, seed=True)

    def test_write_compose_file_round_trips(self):
        with tempfile.TemporaryDirectory() as home:
            path = jump_config.write_compose_file(
                Path(home), [self.KEY, self.KEY2], seed=True
            )
            self.assertEqual(path, jump_config.paths(Path(home))["compose_file"])
            self.assertIn("djinn-net", path.read_text(encoding="utf-8"))
            # The key file must exist by the time compose reads the overlay.
            keys_path = jump_config.paths(Path(home))["authorized_keys"]
            self.assertTrue(keys_path.is_file())
            self.assertIn(self.KEY2, keys_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
