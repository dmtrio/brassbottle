#!/usr/bin/env python3
"""Unit tests for src/migrate.py — the dev-agent- → djinn- volume migrator.

rebrand-transitional tool — delete this file (alongside src/migrate.py and
the `./djinn migrate` subcommand) once every container has been migrated.

No real docker calls: every subprocess.run() is mocked via a small command
dispatcher (fake_run) keyed on the docker subcommand, so each test controls
exactly what "docker" would have said without a daemon anywhere nearby.
Covers: prefix mapping/discovery, the none-found hard error, the
target-already-exists skip (never overwrites), --dry-run executing no
mutating command, the exact shape of the copy command, and that the
--dry-run text is derived from the same command builders the real executor
uses (so the two can't drift apart).
"""

import subprocess
import sys
import unittest
import unittest.mock
from io import StringIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
import migrate


def _completed(cmd, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=cmd, returncode=returncode, stdout=stdout, stderr=stderr)


class FakeDocker:
    """Dispatches a mocked subprocess.run() by docker subcommand. `volumes`
    is the fake `docker volume ls` universe; `existing` is the set of
    volume names `docker volume inspect` reports as already present;
    `fail_create`/`fail_copy` name volumes whose create/copy step errors."""

    def __init__(self, volumes=(), existing=(), fail_create=(), fail_copy=(),
                 stop_ok=True):
        self.volumes = list(volumes)
        self.existing = set(existing)
        self.fail_create = set(fail_create)
        self.fail_copy = set(fail_copy)
        self.stop_ok = stop_ok
        self.calls = []  # every command, in order — asserted against directly

    def __call__(self, cmd, **_kwargs):
        self.calls.append(cmd)
        if cmd[:3] == ["docker", "volume", "ls"]:
            return _completed(cmd, stdout="\n".join(self.volumes) + ("\n" if self.volumes else ""))
        if cmd[:2] == ["docker", "stop"]:
            return _completed(cmd, returncode=0 if self.stop_ok else 1,
                               stderr="" if self.stop_ok else "No such container")
        if cmd[:3] == ["docker", "volume", "inspect"]:
            name = cmd[3]
            ok = name in self.existing
            return _completed(cmd, returncode=0 if ok else 1,
                               stderr="" if ok else f"no such volume: {name}")
        if cmd[:3] == ["docker", "volume", "create"]:
            name = cmd[3]
            if name in self.fail_create:
                return _completed(cmd, returncode=1, stderr=f"create failed: {name}")
            return _completed(cmd, returncode=0)
        if cmd[:2] == ["docker", "run"]:
            # -v <old>:/from:ro is argv[4]; the old volume name is its prefix.
            old = cmd[4].split(":")[0]
            if old in self.fail_copy:
                return _completed(cmd, returncode=1, stderr=f"cp failed for {old}")
            return _completed(cmd, returncode=0)
        raise AssertionError(f"unexpected command: {cmd}")


class QuietTestCase(unittest.TestCase):
    """Silences migrate's stdout/stderr prints so test output stays clean;
    subclasses can still inspect self.out/self.err if they want to."""

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


class TargetNameTests(unittest.TestCase):
    def test_maps_dev_agent_prefix_to_djinn(self):
        self.assertEqual(
            migrate.target_name("dev-agent-mysite_workspace", "mysite"),
            "djinn-mysite_workspace",
        )

    def test_preserves_underscores_in_suffix(self):
        self.assertEqual(
            migrate.target_name("dev-agent-mysite_claude_auth", "mysite"),
            "djinn-mysite_claude_auth",
        )


class DiscoveryAndPlanTests(QuietTestCase):
    def test_plan_filters_by_prefix_and_maps_targets(self):
        fake = FakeDocker(volumes=[
            "dev-agent-mysite_workspace",
            "dev-agent-mysite_claude-auth",
            "dev-agent-othersite_workspace",  # different container — excluded
            "unrelated-volume",
        ])
        with unittest.mock.patch.object(migrate.subprocess, "run", side_effect=fake):
            mapping = migrate.plan_migration("mysite")
        self.assertEqual(mapping, [
            ("dev-agent-mysite_claude-auth", "djinn-mysite_claude-auth"),
            ("dev-agent-mysite_workspace", "djinn-mysite_workspace"),
        ])

    def test_plan_is_sorted_regardless_of_docker_output_order(self):
        fake = FakeDocker(volumes=["dev-agent-x_b", "dev-agent-x_a"])
        with unittest.mock.patch.object(migrate.subprocess, "run", side_effect=fake):
            mapping = migrate.plan_migration("x")
        self.assertEqual([old for old, _ in mapping], ["dev-agent-x_a", "dev-agent-x_b"])

    def test_docker_volume_ls_failure_raises(self):
        def boom(cmd, **_kwargs):
            return _completed(cmd, returncode=1, stderr="docker daemon not running")
        with unittest.mock.patch.object(migrate.subprocess, "run", side_effect=boom):
            with self.assertRaises(migrate.MigrateError) as cm:
                migrate.plan_migration("mysite")
        self.assertIn("docker daemon not running", str(cm.exception))


class NoneFoundTests(QuietTestCase):
    def test_no_matching_volumes_is_a_clear_error_exit_1(self):
        fake = FakeDocker(volumes=["dev-agent-othersite_workspace"])
        with unittest.mock.patch.object(migrate.subprocess, "run", side_effect=fake):
            rc = migrate.migrate("mysite", dry_run=False)
        self.assertEqual(rc, 1)
        self.assertIn("no volumes found", self.err)
        self.assertIn("mysite", self.err)
        # Nothing beyond the discovery listing ran — no stop, no create, no copy.
        self.assertEqual(len(fake.calls), 1)

    def test_empty_docker_volume_ls_output_is_also_none_found(self):
        fake = FakeDocker(volumes=[])
        with unittest.mock.patch.object(migrate.subprocess, "run", side_effect=fake):
            rc = migrate.migrate("mysite", dry_run=False)
        self.assertEqual(rc, 1)


class TargetExistsSkipTests(QuietTestCase):
    def test_existing_target_is_skipped_never_overwritten(self):
        fake = FakeDocker(
            volumes=["dev-agent-mysite_workspace", "dev-agent-mysite_claude-auth"],
            existing={"djinn-mysite_workspace"},  # already migrated once
        )
        with unittest.mock.patch.object(migrate.subprocess, "run", side_effect=fake):
            rc = migrate.migrate("mysite", dry_run=False)
        self.assertEqual(rc, 0)
        self.assertIn("already exists", self.out)
        self.assertIn("skipping", self.out)
        # No `docker volume create djinn-mysite_workspace` — the skip path
        # never touches the already-existing target.
        create_targets = [c[3] for c in fake.calls if c[:3] == ["docker", "volume", "create"]]
        self.assertNotIn("djinn-mysite_workspace", create_targets)
        self.assertIn("djinn-mysite_claude-auth", create_targets)
        self.assertIn("Done: 1 volume(s) copied, 1 skipped", self.out)


class DryRunTests(QuietTestCase):
    def test_dry_run_executes_no_mutating_command(self):
        fake = FakeDocker(volumes=["dev-agent-mysite_workspace"])
        with unittest.mock.patch.object(migrate.subprocess, "run", side_effect=fake):
            rc = migrate.migrate("mysite", dry_run=True)
        self.assertEqual(rc, 0)
        # Only the read-only discovery listing may have run — no stop, no
        # create, no copy: --dry-run means "print the plan, execute nothing".
        self.assertEqual(fake.calls, [["docker", "volume", "ls", "--format", "{{.Name}}"]])
        self.assertIn("--dry-run: plan only, nothing executed", self.out)
        self.assertIn("djinn-mysite_workspace", self.out)

    def test_dry_run_still_reports_none_found_as_an_error(self):
        fake = FakeDocker(volumes=[])
        with unittest.mock.patch.object(migrate.subprocess, "run", side_effect=fake):
            rc = migrate.migrate("mysite", dry_run=True)
        self.assertEqual(rc, 1)
        self.assertIn("no volumes found", self.err)


class CopyCommandShapeTests(QuietTestCase):
    def test_copy_command_matches_the_documented_shape(self):
        fake = FakeDocker(volumes=["dev-agent-mysite_workspace"])
        with unittest.mock.patch.object(migrate.subprocess, "run", side_effect=fake):
            migrate.migrate("mysite", dry_run=False)
        run_cmds = [c for c in fake.calls if c[:2] == ["docker", "run"]]
        self.assertEqual(len(run_cmds), 1)
        self.assertEqual(run_cmds[0], [
            "docker", "run", "--rm",
            "-v", "dev-agent-mysite_workspace:/from:ro",
            "-v", "djinn-mysite_workspace:/to",
            "busybox", "sh", "-c", "cp -a /from/. /to/",
        ])

    def test_create_precedes_copy_for_each_volume(self):
        fake = FakeDocker(volumes=["dev-agent-mysite_workspace"])
        with unittest.mock.patch.object(migrate.subprocess, "run", side_effect=fake):
            migrate.migrate("mysite", dry_run=False)
        kinds = [c[1] for c in fake.calls if c[0] == "docker"]
        # ls, stop, volume-inspect(=volume), volume-create(=volume), run
        self.assertEqual(kinds, ["volume", "stop", "volume", "volume", "run"])


class StopOldContainerTests(QuietTestCase):
    def test_stop_failure_is_non_fatal(self):
        fake = FakeDocker(volumes=["dev-agent-mysite_workspace"], stop_ok=False)
        with unittest.mock.patch.object(migrate.subprocess, "run", side_effect=fake):
            rc = migrate.migrate("mysite", dry_run=False)
        self.assertEqual(rc, 0)
        self.assertIn("skipped: dev-agent-mysite not running or not found", self.out)

    def test_stop_targets_the_old_prefixed_container_name(self):
        fake = FakeDocker(volumes=["dev-agent-mysite_workspace"])
        with unittest.mock.patch.object(migrate.subprocess, "run", side_effect=fake):
            migrate.migrate("mysite", dry_run=False)
        stop_cmds = [c for c in fake.calls if c[:2] == ["docker", "stop"]]
        self.assertEqual(stop_cmds, [["docker", "stop", "dev-agent-mysite"]])


class FailureSurfacesCommandTests(QuietTestCase):
    def test_create_failure_exits_nonzero_and_names_the_command(self):
        fake = FakeDocker(volumes=["dev-agent-mysite_workspace"],
                           fail_create={"djinn-mysite_workspace"})
        with unittest.mock.patch.object(migrate.subprocess, "run", side_effect=fake):
            rc = migrate.migrate("mysite", dry_run=False)
        self.assertEqual(rc, 1)
        self.assertIn("docker volume create djinn-mysite_workspace failed", self.err)

    def test_copy_failure_exits_nonzero_and_names_the_command(self):
        fake = FakeDocker(volumes=["dev-agent-mysite_workspace"],
                           fail_copy={"dev-agent-mysite_workspace"})
        with unittest.mock.patch.object(migrate.subprocess, "run", side_effect=fake):
            rc = migrate.migrate("mysite", dry_run=False)
        self.assertEqual(rc, 1)
        self.assertIn("copy failed", self.err)
        self.assertIn("docker run --rm", self.err)

    def test_old_volume_is_never_deleted_or_modified(self):
        # No test double ever wires up a `docker volume rm`/`docker volume
        # update` handler — FakeDocker.__call__ raises AssertionError on any
        # command it doesn't recognize, so this whole suite already proves
        # the old volume is never touched. This test pins that guarantee by
        # name for a run with a skip AND a copy in the same batch.
        fake = FakeDocker(
            volumes=["dev-agent-mysite_a", "dev-agent-mysite_b"],
            existing={"djinn-mysite_a"},
        )
        with unittest.mock.patch.object(migrate.subprocess, "run", side_effect=fake):
            rc = migrate.migrate("mysite", dry_run=False)
        self.assertEqual(rc, 0)
        rm_calls = [c for c in fake.calls if "rm" in c]
        self.assertEqual(rm_calls, [])


class DryRunDerivationTests(QuietTestCase):
    """--dry-run's printed plan must come from the same command builders the
    real executor calls — pins that against drift by patching a builder and
    checking BOTH the dry-run text and the real executed argv change."""

    def test_patching_a_builder_changes_dry_run_and_real_execution_alike(self):
        patched_stop = ["docker", "stop", "PATCHED-CONTAINER"]
        with unittest.mock.patch.object(migrate, "stop_cmd", return_value=patched_stop):
            fake_dry = FakeDocker(volumes=["dev-agent-mysite_workspace"])
            with unittest.mock.patch.object(migrate.subprocess, "run", side_effect=fake_dry):
                rc_dry = migrate.migrate("mysite", dry_run=True)
            self.assertEqual(rc_dry, 0)
            self.assertIn("PATCHED-CONTAINER", self.out)

            fake_real = FakeDocker(volumes=["dev-agent-mysite_workspace"])
            with unittest.mock.patch.object(migrate.subprocess, "run", side_effect=fake_real):
                rc_real = migrate.migrate("mysite", dry_run=False)
            self.assertEqual(rc_real, 0)
        stop_calls = [c for c in fake_real.calls if c[:2] == ["docker", "stop"]]
        self.assertEqual(stop_calls, [patched_stop])

    def test_dry_run_copy_line_matches_the_real_copy_argv(self):
        """The --dry-run print for the copy step is exactly copy_cmd()'s
        output joined with spaces — not a hand-typed second rendering of it."""
        fake = FakeDocker(volumes=["dev-agent-mysite_workspace"])
        with unittest.mock.patch.object(migrate.subprocess, "run", side_effect=fake):
            migrate.migrate("mysite", dry_run=True)
        expected = migrate.copy_cmd("dev-agent-mysite_workspace", "djinn-mysite_workspace")
        self.assertIn(migrate._format_cmd(expected), self.out)


class MainTests(QuietTestCase):
    def test_main_wires_dry_run_flag(self):
        fake = FakeDocker(volumes=["dev-agent-mysite_workspace"])
        with unittest.mock.patch.object(migrate.subprocess, "run", side_effect=fake):
            rc = migrate.main(["mysite", "--dry-run"])
        self.assertEqual(rc, 0)
        self.assertEqual(fake.calls, [["docker", "volume", "ls", "--format", "{{.Name}}"]])

    def test_main_requires_name(self):
        with self.assertRaises(SystemExit):
            migrate.main([])


if __name__ == "__main__":
    unittest.main()
