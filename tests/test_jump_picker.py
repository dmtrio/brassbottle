"""Tests for the jump-side picker without Docker, SSH, or a real terminal."""

import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent / "jump"))
import picker  # noqa: E402


class ReadNamesTests(unittest.TestCase):
    def test_filters_untrusted_registry_lines(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bottles"
            path.write_text("djinn-good\n../bad\ndjinn-also_good\n")
            self.assertEqual(picker.read_names(path), ["djinn-good", "djinn-also_good"])


class SelectTests(unittest.TestCase):
    def test_selects_a_numbered_bottle(self):
        output = io.StringIO()
        self.assertEqual(
            picker.select(["djinn-a", "djinn-b"], input_fn=lambda _: "2", output=output),
            "djinn-b",
        )
        self.assertIn("1. a", output.getvalue())

    def test_reprompts_after_invalid_selection(self):
        choices = iter(["no", "q"])
        output = io.StringIO()
        self.assertIsNone(
            picker.select(["djinn-a"], input_fn=lambda _: next(choices), output=output)
        )
        self.assertIn("Choose a listed number or q.", output.getvalue())

    def test_empty_list_surfaces_recovery_message(self):
        output = io.StringIO()
        self.assertIsNone(picker.select([], output=output))
        self.assertIn("No running jump-reachable bottles", output.getvalue())

    def test_eof_stays_on_jump(self):
        output = io.StringIO()
        def eof(_):
            raise EOFError

        self.assertIsNone(picker.select(["djinn-a"], input_fn=eof, output=output))

    def test_interrupt_stays_on_jump(self):
        def interrupt(_):
            raise KeyboardInterrupt

        self.assertIsNone(picker.select(["djinn-a"], input_fn=interrupt, output=io.StringIO()))


class HopTests(unittest.TestCase):
    def test_hop_waits_for_ssh_instead_of_replacing_picker_process(self):
        with mock.patch.object(picker.subprocess, "call", return_value=0) as call:
            self.assertEqual(picker.hop("djinn-a"), 0)
        call.assert_called_once_with(["ssh", "djinn-a"])

    def test_hop_keeps_picker_alive_when_ssh_cannot_start(self):
        with mock.patch.object(picker.subprocess, "call", side_effect=OSError):
            self.assertIsNone(picker.hop("djinn-a"))

    def test_hop_keeps_picker_alive_when_ssh_is_cancelled(self):
        with mock.patch.object(picker.subprocess, "call", side_effect=KeyboardInterrupt):
            self.assertIsNone(picker.hop("djinn-a"))


if __name__ == "__main__":
    unittest.main()
