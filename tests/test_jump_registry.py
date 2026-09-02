"""Tests for the host-generated, jump-mounted bottle registry."""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
import jump_config  # noqa: E402
import jump_registry  # noqa: E402


def completed(command, returncode=0, stdout=""):
    return subprocess.CompletedProcess(command, returncode, stdout=stdout, stderr="")


class RunningBottlesTests(unittest.TestCase):
    def test_queries_running_jump_enabled_containers_and_sorts(self):
        result = completed([], stdout="djinn-z\ndjinn-a\ndjinn-z\n")
        with tempfile.TemporaryDirectory() as home, mock.patch(
            "subprocess.run", return_value=result
        ) as run:
            scope = jump_config.derive_identity(Path(home)).suffix
            self.assertEqual(jump_registry.running_bottles(Path(home)), ["djinn-a", "djinn-z"])
        self.assertEqual(
            run.call_args.args[0],
            [
                "docker", "ps", "--filter", "label=djinn.remote.jump=true",
                "--filter", f"label=djinn.jump.scope={scope}", "--format", "{{.Names}}",
            ],
        )

    def test_rejects_an_unexpected_container_name(self):
        with tempfile.TemporaryDirectory() as home, mock.patch(
            "subprocess.run", return_value=completed([], stdout="not-djinn\n")
        ):
            with self.assertRaises(jump_registry.JumpRegistryError):
                jump_registry.running_bottles(Path(home))

    def test_surfaces_docker_failure(self):
        with tempfile.TemporaryDirectory() as home, mock.patch(
            "subprocess.run", return_value=completed([], returncode=7)
        ):
            with self.assertRaises(jump_registry.JumpRegistryError):
                jump_registry.running_bottles(Path(home))


class WriteRegistryTests(unittest.TestCase):
    def test_writes_sorted_validated_registry(self):
        with tempfile.TemporaryDirectory() as home:
            path = jump_registry.write_registry(Path(home), ["djinn-z", "djinn-a", "djinn-a"])
            self.assertEqual(path.read_text(), "djinn-a\ndjinn-z\n")
            self.assertEqual(path, jump_config.paths(Path(home))["registry_file"])
            self.assertEqual(path.stat().st_mode & 0o777, 0o644)

    def test_rejects_invalid_name_before_writing(self):
        with tempfile.TemporaryDirectory() as home:
            with self.assertRaises(jump_registry.JumpRegistryError):
                jump_registry.write_registry(Path(home), ["../bad"])
            self.assertFalse(jump_config.paths(Path(home))["registry_file"].exists())


if __name__ == "__main__":
    unittest.main()
