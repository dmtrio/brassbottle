#!/usr/bin/env python3
"""Unit tests for src/bottle_net.py — per-bottle subnet and port allocation.

No Docker: the caller-supplied facts arrive as a Taken value. The concurrency
test uses real processes contending on the real flock, not mocks.
"""

import contextlib
import io
import json
import multiprocessing
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
import bottle_net as bn
from bottle_net import Taken


def _worker_alloc(home, name, ssh, out):
    with contextlib.redirect_stderr(io.StringIO()):
        with bn.allocation_lock(Path(home)):
            alloc = bn.allocate_fleet(Path(home), {name: ssh})
    out.put((name, alloc[name]))


def _worker_hold(home, held, release, stamps):
    with contextlib.redirect_stderr(io.StringIO()):
        with bn.allocation_lock(Path(home)):
            stamps.put(("a_in", time.monotonic()))
            held.set()
            release.wait(10)
            stamps.put(("a_out", time.monotonic()))


def _worker_wait(home, held, stamps):
    held.wait(10)
    with contextlib.redirect_stderr(io.StringIO()):
        with bn.allocation_lock(Path(home)):
            stamps.put(("b_in", time.monotonic()))


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self.path = self.home / "run" / "bottle-nets.json"
        self.env = {}
        err = contextlib.redirect_stderr(io.StringIO())
        err.__enter__()
        self.addCleanup(err.__exit__, None, None, None)

    def alloc(self, fleet, taken=None, env=None):
        with bn.allocation_lock(self.home):
            return bn.allocate_fleet(self.home, fleet, taken, env or self.env)

    def snapshot(self):
        return self.path.read_bytes() if self.path.exists() else None

    def refuses(self, exc, fleet, taken=None, env=None):
        before = self.snapshot()
        with self.assertRaises(exc):
            self.alloc(fleet, taken, env)
        self.assertEqual(self.snapshot(), before, "file changed on refusal")


class Determinism(Base):
    def test_first_fit_sorted_literal(self):
        out = self.alloc({"zed": None, "alpha": 2222, "mid": None})
        self.assertEqual(
            out,
            {
                "alpha": {"subnet": "172.31.0.0/28", "browser_port": 8900, "ssh_port": 2222},
                "mid": {"subnet": "172.31.0.16/28", "browser_port": 8901},
                "zed": {"subnet": "172.31.0.32/28", "browser_port": 8902},
            },
        )

    def test_input_order_irrelevant_and_repeatable(self):
        a = self.alloc({"b": None, "a": None, "c": None})
        raw = self.snapshot()
        mtime = self.path.stat().st_mtime_ns
        b = self.alloc({"c": None, "a": None, "b": None})
        self.assertEqual(a, b)
        self.assertEqual(self.snapshot(), raw)
        self.assertEqual(self.path.stat().st_mtime_ns, mtime, "unchanged run rewrote")

    def test_existing_entry_never_moves_when_fleet_grows(self):
        self.alloc({"m": None})
        out = self.alloc({"a": None, "m": None})
        self.assertEqual(out["m"]["subnet"], "172.31.0.0/28")
        self.assertEqual(out["a"]["subnet"], "172.31.0.16/28")

    def test_custom_pool_and_ports(self):
        env = {"DJINN_BOTTLE_POOL": "10.9.0.0/24", "DJINN_BROWSER_PROXY_PORTS": "7000-7001"}
        out = self.alloc({"a": None, "b": None}, env=env)
        self.assertEqual(out["a"], {"subnet": "10.9.0.0/28", "browser_port": 7000})
        self.assertEqual(out["b"], {"subnet": "10.9.0.16/28", "browser_port": 7001})
        self.refuses(bn.ExhaustedError, {"a": None, "b": None, "c": None}, env=env)

    def test_bad_config_refused(self):
        for env in (
            {"DJINN_BOTTLE_POOL": "nonsense"},
            {"DJINN_BOTTLE_POOL": "10.0.0.0/30"},
            {"DJINN_BROWSER_PROXY_PORTS": "9-3"},
            {"DJINN_BROWSER_PROXY_PORTS": "x"},
        ):
            self.refuses(bn.ConfigError, {"a": None}, env=env)

    def test_pool_exhaustion(self):
        env = {"DJINN_BOTTLE_POOL": "10.9.0.0/27"}
        self.alloc({"a": None, "b": None}, env=env)
        self.refuses(bn.ExhaustedError, {"a": None, "b": None, "c": None}, env=env)


class Concurrency(Base):
    def test_two_processes_under_lock_get_distinct_entries(self):
        ctx = multiprocessing.get_context("fork")
        out = ctx.Queue()
        procs = [
            ctx.Process(target=_worker_alloc, args=(str(self.home), f"b{i}", 2200 + i, out))
            for i in range(6)
        ]
        for p in procs:
            p.start()
        results = dict(out.get(timeout=30) for _ in procs)
        for p in procs:
            p.join(30)
            self.assertEqual(p.exitcode, 0)
        subnets = {v["subnet"] for v in results.values()}
        ports = {v["browser_port"] for v in results.values()}
        self.assertEqual(len(subnets), 6)
        self.assertEqual(len(ports), 6)
        on_disk = json.loads(self.path.read_text())
        self.assertEqual(sorted(on_disk), [f"b{i}" for i in range(6)])
        self.assertEqual({v["subnet"] for v in on_disk.values()}, subnets)

    def test_second_process_blocks_until_first_releases(self):
        ctx = multiprocessing.get_context("fork")
        held, release, stamps = ctx.Event(), ctx.Event(), ctx.Queue()
        a = ctx.Process(target=_worker_hold, args=(str(self.home), held, release, stamps))
        b = ctx.Process(target=_worker_wait, args=(str(self.home), held, stamps))
        a.start()
        b.start()
        self.assertTrue(held.wait(10))
        time.sleep(0.3)
        self.assertTrue(b.is_alive(), "second process did not block on the lock")
        release.set()
        a.join(10)
        b.join(10)
        got = dict(stamps.get(timeout=5) for _ in range(3))
        self.assertGreaterEqual(got["b_in"], got["a_out"])

    def test_timeout(self):
        with bn.allocation_lock(self.home):
            ctx = multiprocessing.get_context("fork")
            q = ctx.Queue()

            def probe(q):
                try:
                    with contextlib.redirect_stderr(io.StringIO()):
                        with bn.allocation_lock(self.home, timeout=0.2):
                            q.put("got")
                except bn.LockError:
                    q.put("timeout")

            p = ctx.Process(target=probe, args=(q,))
            p.start()
            self.assertEqual(q.get(timeout=10), "timeout")
            p.join(10)

    def test_not_reentrant_and_operations_need_the_lock(self):
        with self.assertRaises(bn.LockError):
            bn.allocate_fleet(self.home, {"a": None})
        with self.assertRaises(bn.LockError):
            bn.purge(self.home, "a")
        with bn.allocation_lock(self.home):
            with self.assertRaises(bn.LockError):
                with bn.allocation_lock(self.home):
                    pass
        with bn.allocation_lock(self.home):  # released cleanly afterwards
            pass


class Collisions(Base):
    def setUp(self):
        super().setUp()
        self.alloc({"keep": 2222})
        self.assertIsNotNone(self.snapshot())

    def test_each_class_leaves_file_identical(self):
        fleet = {"keep": 2222, "new": 2223}
        cases = [
            (bn.PoolConflictError, Taken(networks={"bridge": "172.31.0.0/16"})),
            (bn.PoolConflictError, Taken(networks={"big": "172.16.0.0/12"})),
            (bn.CollisionError, Taken(grants=["172.31.0.0/24"])),
            (bn.CollisionError, Taken(grants=["172.31.0.5"])),
            (bn.CollisionError, Taken(plugin_ports=[2223])),
            (bn.CollisionError, Taken(service_ports=[2223])),
            (bn.CollisionError, Taken(listeners=[2223])),
            (bn.CollisionError, Taken(listeners=[8900])),  # keep's browser_port
            (bn.CollisionError, Taken(plugin_ports=[2222])),  # keep's ssh_port
        ]
        for exc, taken in cases:
            with self.subTest(taken=taken):
                self.refuses(exc, fleet, taken)

    def test_new_bottle_skips_taken_ports_and_subnets(self):
        taken = Taken(
            grants=["172.31.0.16/28"],
            networks={"djinn-b-other": "172.31.0.32/28"},
            listeners=[8901],
            service_ports=[8902],
            plugin_ports=[8903],
        )
        out = self.alloc({"keep": 2222, "new": None}, taken)
        self.assertEqual(out["new"], {"subnet": "172.31.0.48/28", "browser_port": 8904})

    def test_own_network_does_not_collide_with_itself(self):
        out = self.alloc({"keep": 2222}, Taken(networks={"djinn-b-keep": "172.31.0.0/28"}))
        self.assertEqual(out["keep"]["subnet"], "172.31.0.0/28")

    def test_other_bottles_network_collides(self):
        self.refuses(bn.CollisionError, {"keep": 2222}, Taken(networks={"djinn-b-x": "172.31.0.0/28"}))

    def test_ssh_port_clashes_between_bottles(self):
        self.refuses(bn.CollisionError, {"keep": 2222, "new": 2222})
        self.refuses(bn.CollisionError, {"keep": 2222, "new": 8900})  # keep's browser port

    def test_new_browser_port_avoids_requested_ssh_ports(self):
        out = self.alloc({"keep": 2222, "new": 8901, "other": None})
        self.assertEqual(out["new"]["browser_port"], 8902)
        self.assertEqual(out["other"]["browser_port"], 8903)

    def test_ssh_port_changes_and_clears_with_manifest(self):
        out = self.alloc({"keep": 2300})
        self.assertEqual(out["keep"]["ssh_port"], 2300)
        out = self.alloc({"keep": None})
        self.assertNotIn("ssh_port", out["keep"])

    def test_existing_entry_outside_changed_pool_refused(self):
        self.refuses(bn.CollisionError, {"keep": 2222}, env={"DJINN_BOTTLE_POOL": "10.9.0.0/24"})
        self.refuses(bn.CollisionError, {"keep": 2222}, env={"DJINN_BROWSER_PROXY_PORTS": "9000-9010"})

    def test_bad_port_input_refused(self):
        self.refuses(bn.ConfigError, {"keep": 70000})
        self.refuses(bn.ConfigError, {"keep": 2222}, Taken(listeners=["80"]))

    def test_fleet_with_one_bad_member_writes_nothing_for_the_rest(self):
        self.refuses(bn.CollisionError, {"keep": 2222, "good": None, "bad": 8900})

    def test_corrupt_file_refused_and_untouched(self):
        for bad in (b"{", b"[]", b'{"a": {"subnet": "x", "browser_port": 1}}',
                    b'{"a": {"subnet": "172.31.0.0/28"}}'):
            self.path.write_bytes(bad)
            self.refuses(bn.CorruptFileError, {"a": None})


class Pool(Base):
    def test_pool_overlapping_foreign_network_refused(self):
        self.refuses(bn.PoolConflictError, {"a": None}, Taken(networks={"legacy": "172.31.4.0/24"}))
        self.assertIsNone(self.snapshot())

    def test_djinn_networks_inside_pool_are_fine(self):
        out = self.alloc({"a": None}, Taken(networks={"djinn-b-z": "172.31.0.0/28"}))
        self.assertEqual(out["a"]["subnet"], "172.31.0.16/28")


class Purge(Base):
    def test_purge_frees_and_reuses(self):
        self.alloc({"a": None, "b": 2222, "c": None})
        with bn.allocation_lock(self.home):
            self.assertTrue(bn.purge(self.home, "b"))
        self.assertEqual(sorted(json.loads(self.path.read_text())), ["a", "c"])
        out = self.alloc({"a": None, "c": None, "d": 2222})
        self.assertEqual(out["d"], {"subnet": "172.31.0.16/28", "browser_port": 8901, "ssh_port": 2222})
        self.assertEqual(out["a"]["subnet"], "172.31.0.0/28")

    def test_purge_absent_changes_nothing(self):
        self.alloc({"a": None})
        before = self.snapshot()
        with bn.allocation_lock(self.home):
            self.assertFalse(bn.purge(self.home, "nope"))
        self.assertEqual(self.snapshot(), before)

    def test_purge_with_no_file(self):
        with bn.allocation_lock(self.home):
            self.assertFalse(bn.purge(self.home, "a"))
        self.assertIsNone(self.snapshot())


class Files(Base):
    def test_atomic_write_leaves_no_temp_and_is_sorted_json(self):
        self.alloc({"b": None, "a": None})
        self.assertEqual([p.name for p in self.path.parent.glob(".*.tmp")], [])
        self.assertEqual(self.snapshot(), bn._encode(json.loads(self.snapshot())))

    def test_failed_replace_keeps_old_file_and_cleans_temp(self):
        self.alloc({"a": None})
        before = self.snapshot()
        orig = bn.os.replace

        def boom(*a, **k):
            raise OSError("disk")

        bn.os.replace = boom
        try:
            with self.assertRaises(OSError):
                self.alloc({"a": None, "b": None})
        finally:
            bn.os.replace = orig
        self.assertEqual(self.snapshot(), before)
        self.assertEqual([p.name for p in self.path.parent.glob(".*.tmp")], [])

    def test_boundary_logs_stage_duration_and_sizes(self):
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            self.alloc({"a": None})
        text = buf.getvalue()
        for needle in ("stage=lock", "stage=load", "stage=write", "stage=allocate",
                       "stage=unlock", "ms=", "bytes_out="):
            self.assertIn(needle, text)

    def test_resolve_home(self):
        self.assertEqual(bn.resolve_home({"DJINN_HOME": "/x/y"}), Path("/x/y"))
        with self.assertRaises(bn.ConfigError):
            bn.resolve_home({})


if __name__ == "__main__":
    unittest.main()
