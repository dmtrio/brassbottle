#!/usr/bin/env python3
"""Unit tests for src/ensure_net.py — the shared djinn-net bridge creator,
extracted out of up.sh's inline network block to fix "pool overlaps" on a
pre-existing dev-agent-net install (SEVERE PR #43 finding).

No real docker calls: every subprocess.run() is mocked via a small command
dispatcher (FakeDocker) keyed on the docker network subcommand, mirroring
tests/test_migrate.py's approach. Covers every branch: djinn-net already
present (matching/mismatched subnet), djinn-net missing with no old net
(create success/race-tolerant/failure), and the rebrand-transitional
dev-agent-net handoff (empty -> reclaimed, attached -> hard abort).
"""

import subprocess
import sys
import unittest
import unittest.mock
from io import StringIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
import ensure_net


def _completed(cmd, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=cmd, returncode=returncode, stdout=stdout, stderr=stderr)


class FakeDocker:
    """Dispatches a mocked subprocess.run() by docker network subcommand.

    `existing` — network names `docker network inspect <name>` reports as
    present. `subnets` — {name: subnet} for the -f Subnet query. `counts` —
    {name: int} for the -f len(.Containers) query. `names` — {name: [container,
    ...]} for the -f range(.Containers) query. `fail_create`/`fail_rm` — names
    whose create/rm step errors.
    """

    def __init__(self, existing=(), subnets=None, counts=None, names=None,
                 fail_create=False, fail_rm=()):
        self.existing = set(existing)
        self.subnets = dict(subnets or {})
        self.counts = dict(counts or {})
        self.names = dict(names or {})
        self.fail_create = fail_create
        self.fail_rm = set(fail_rm)
        self.calls = []  # every command, in order — asserted against directly
        self.created_after_fail = False  # simulates winning a concurrent create race

    def __call__(self, cmd, **_kwargs):
        self.calls.append(cmd)
        if cmd[:3] == ["docker", "network", "inspect"]:
            if cmd[3] == "-f":
                fmt, name = cmd[4], cmd[5]
                if name not in self.existing:
                    return _completed(cmd, returncode=1, stderr=f"no such network: {name}")
                if "Subnet" in fmt:
                    return _completed(cmd, stdout=self.subnets.get(name, "") + "\n")
                if "len" in fmt:
                    return _completed(cmd, stdout=str(self.counts.get(name, 0)) + "\n")
                if "range" in fmt:
                    joined = " ".join(self.names.get(name, []))
                    return _completed(cmd, stdout=(joined + " \n") if joined else "\n")
                raise AssertionError(f"unexpected inspect format: {fmt}")
            name = cmd[3]
            ok = name in self.existing
            return _completed(cmd, returncode=0 if ok else 1,
                               stderr="" if ok else f"no such network: {name}")
        if cmd[:3] == ["docker", "network", "rm"]:
            name = cmd[3]
            if name in self.fail_rm:
                return _completed(cmd, returncode=1, stderr=f"rm failed: {name}")
            self.existing.discard(name)
            return _completed(cmd, returncode=0)
        if cmd[:3] == ["docker", "network", "create"]:
            if self.fail_create:
                return _completed(cmd, returncode=1, stderr="pool overlaps")
            self.existing.add(ensure_net.NET_NAME)
            return _completed(cmd, returncode=0)
        raise AssertionError(f"unexpected command: {cmd}")


class QuietTestCase(unittest.TestCase):
    """Silences ensure_net's stdout/stderr prints so test output stays clean;
    subclasses can still inspect self.out/self.err."""

    def setUp(self):
        self._out, self._err = StringIO(), StringIO()
        self._out_patch = unittest.mock.patch("sys.stdout", self._out)
        self._err_patch = unittest.mock.patch("sys.stderr", self._err)
        self._out_patch.start()
        self._err_patch.start()
        self.addCleanup(self._out_patch.stop)
        self.addCleanup(self._err_patch.stop)

    @property
    def out(self):
        return self._out.getvalue()

    @property
    def err(self):
        return self._err.getvalue()


class DjinnNetExistsTests(QuietTestCase):
    def test_matching_subnet_is_silent_success(self):
        fake = FakeDocker(existing={"djinn-net"}, subnets={"djinn-net": "172.30.0.0/24"})
        with unittest.mock.patch.object(ensure_net.subprocess, "run", side_effect=fake):
            rc = ensure_net.ensure_net("172.30.0.0/24")
        self.assertEqual(rc, 0)
        self.assertNotIn("already exists with subnet", self.out)
        # Only the existence + subnet checks ran — no create, no old-net probe.
        self.assertEqual(len(fake.calls), 2)

    def test_mismatched_subnet_warns_but_still_succeeds(self):
        fake = FakeDocker(existing={"djinn-net"}, subnets={"djinn-net": "172.31.0.0/24"})
        with unittest.mock.patch.object(ensure_net.subprocess, "run", side_effect=fake):
            rc = ensure_net.ensure_net("172.30.0.0/24")
        self.assertEqual(rc, 0)
        self.assertIn("already exists with subnet 172.31.0.0/24", self.out)
        self.assertIn("config wants 172.30.0.0/24", self.out)
        self.assertIn("docker network rm djinn-net", self.out)

    def test_unreadable_subnet_is_not_a_mismatch_warning(self):
        # inspect -f succeeds but the subnet is blank (unexpected IPAM shape)
        # -> treated as "unknown", not compared, no false warning.
        fake = FakeDocker(existing={"djinn-net"}, subnets={"djinn-net": ""})
        with unittest.mock.patch.object(ensure_net.subprocess, "run", side_effect=fake):
            rc = ensure_net.ensure_net("172.30.0.0/24")
        self.assertEqual(rc, 0)
        self.assertNotIn("already exists with subnet", self.out)


class NoOldNetTests(QuietTestCase):
    def test_missing_both_creates_djinn_net(self):
        fake = FakeDocker(existing=set())
        with unittest.mock.patch.object(ensure_net.subprocess, "run", side_effect=fake):
            rc = ensure_net.ensure_net("172.30.0.0/24")
        self.assertEqual(rc, 0)
        self.assertIn("Creating shared network djinn-net (172.30.0.0/24)", self.out)
        create_calls = [c for c in fake.calls if c[:3] == ["docker", "network", "create"]]
        self.assertEqual(create_calls, [["docker", "network", "create", "--subnet",
                                          "172.30.0.0/24", "djinn-net"]])

    def test_create_failure_is_a_hard_error_with_the_subnet_hint(self):
        fake = FakeDocker(existing=set(), fail_create=True)
        with unittest.mock.patch.object(ensure_net.subprocess, "run", side_effect=fake):
            rc = ensure_net.ensure_net("172.30.0.0/24")
        self.assertEqual(rc, 1)
        self.assertIn("could not create djinn-net (172.30.0.0/24)", self.err)
        self.assertIn("DJINN_SUBNET", self.err)

    def test_create_failure_but_network_now_exists_tolerates_the_race(self):
        # A concurrent up.sh run won the create race: our create call fails,
        # but a follow-up inspect finds djinn-net anyway -> not an error.
        fake = FakeDocker(existing=set(), fail_create=True)

        def side_effect(cmd, **kwargs):
            result = fake(cmd, **kwargs)
            if cmd[:3] == ["docker", "network", "create"]:
                fake.existing.add("djinn-net")  # the race winner's network appears
            return result

        with unittest.mock.patch.object(ensure_net.subprocess, "run", side_effect=side_effect):
            rc = ensure_net.ensure_net("172.30.0.0/24")
        self.assertEqual(rc, 0)
        self.assertEqual(self.err, "")


class OldNetHandoffTests(QuietTestCase):
    """rebrand-transitional: djinn-net missing, dev-agent-net still around."""

    def test_empty_old_net_is_removed_then_djinn_net_created(self):
        fake = FakeDocker(existing={"dev-agent-net"}, counts={"dev-agent-net": 0})
        with unittest.mock.patch.object(ensure_net.subprocess, "run", side_effect=fake):
            rc = ensure_net.ensure_net("172.30.0.0/24")
        self.assertEqual(rc, 0)
        self.assertIn("no attached containers", self.out)
        rm_calls = [c for c in fake.calls if c[:3] == ["docker", "network", "rm"]]
        self.assertEqual(rm_calls, [["docker", "network", "rm", "dev-agent-net"]])
        create_calls = [c for c in fake.calls if c[:3] == ["docker", "network", "create"]]
        self.assertEqual(create_calls, [["docker", "network", "create", "--subnet",
                                          "172.30.0.0/24", "djinn-net"]])
        # rm must happen before create (the subnet has to be freed first).
        self.assertLess(fake.calls.index(rm_calls[0]), fake.calls.index(create_calls[0]))

    def test_rm_failure_on_empty_old_net_is_a_hard_error(self):
        fake = FakeDocker(existing={"dev-agent-net"}, counts={"dev-agent-net": 0},
                           fail_rm={"dev-agent-net"})
        with unittest.mock.patch.object(ensure_net.subprocess, "run", side_effect=fake):
            rc = ensure_net.ensure_net("172.30.0.0/24")
        self.assertEqual(rc, 1)
        self.assertIn("could not remove old network dev-agent-net", self.err)
        # Never reached the create step.
        create_calls = [c for c in fake.calls if c[:3] == ["docker", "network", "create"]]
        self.assertEqual(create_calls, [])

    def test_attached_containers_aborts_and_names_them(self):
        fake = FakeDocker(existing={"dev-agent-net"}, counts={"dev-agent-net": 2},
                           names={"dev-agent-net": ["dev-agent-mysite", "dev-agent-other"]})
        with unittest.mock.patch.object(ensure_net.subprocess, "run", side_effect=fake):
            rc = ensure_net.ensure_net("172.30.0.0/24")
        self.assertEqual(rc, 1)
        self.assertIn("dev-agent-mysite", self.err)
        self.assertIn("dev-agent-other", self.err)
        self.assertIn("djinn migrate", self.err)
        self.assertIn("DJINN_SUBNET", self.err)
        # Never removed the old net, never attempted to create the new one.
        rm_calls = [c for c in fake.calls if c[:3] == ["docker", "network", "rm"]]
        create_calls = [c for c in fake.calls if c[:3] == ["docker", "network", "create"]]
        self.assertEqual(rm_calls, [])
        self.assertEqual(create_calls, [])


class MainTests(QuietTestCase):
    def test_no_args_is_usage_error(self):
        rc = ensure_net.main([])
        self.assertEqual(rc, 1)
        self.assertIn("Usage: ensure_net.py <subnet>", self.err)

    def test_wires_the_subnet_argument_through(self):
        fake = FakeDocker(existing={"djinn-net"}, subnets={"djinn-net": "10.0.0.0/24"})
        with unittest.mock.patch.object(ensure_net.subprocess, "run", side_effect=fake):
            rc = ensure_net.main(["10.0.0.0/24"])
        self.assertEqual(rc, 0)
        self.assertNotIn("already exists with subnet", self.out)


if __name__ == "__main__":
    unittest.main()
