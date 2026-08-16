"""Unit tests for src/freshness.py — the container freshness readout formatter.

Covers the pure formatting logic (ISO in → relative-age string, boundaries,
graceful degradation) and the env-var-then-file stamp lookup, plus a wiring
drift guard so the Dockerfile/up.sh contract that feeds this helper can't
silently break.
"""

import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
import freshness  # noqa: E402

NOW = datetime(2026, 7, 22, 12, 0, 0, tzinfo=timezone.utc)


class ParseIsoTests(unittest.TestCase):
    def test_parses_trailing_z(self):
        dt = freshness.parse_iso("2026-07-08T00:00:00Z")
        self.assertEqual(dt, datetime(2026, 7, 8, tzinfo=timezone.utc))

    def test_parses_fractional_seconds_from_docker_inspect(self):
        dt = freshness.parse_iso("2026-07-08T12:34:56.789012345Z")
        # Docker prints nanoseconds; fromisoformat handles up to microseconds.
        self.assertEqual(dt, freshness.parse_iso("2026-07-08T12:34:56.789012Z"))
        self.assertEqual(dt.tzinfo, timezone.utc)

    def test_parses_explicit_offset_and_normalises_to_utc(self):
        dt = freshness.parse_iso("2026-07-08T07:00:00-05:00")
        self.assertEqual(dt, datetime(2026, 7, 8, 12, 0, 0, tzinfo=timezone.utc))

    def test_naive_stamp_assumed_utc(self):
        dt = freshness.parse_iso("2026-07-08T00:00:00")
        self.assertEqual(dt, datetime(2026, 7, 8, tzinfo=timezone.utc))

    def test_none_and_empty_and_garbage_return_none(self):
        for bad in (None, "", "   ", "not-a-date", "2026-13-99T99:99Z"):
            with self.subTest(bad=bad):
                self.assertIsNone(freshness.parse_iso(bad))


class FormatAgeTests(unittest.TestCase):
    def test_same_day_is_today(self):
        then = datetime(2026, 7, 22, 1, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(freshness.format_age(then, NOW), "today")

    def test_one_day(self):
        then = datetime(2026, 7, 21, 12, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(freshness.format_age(then, NOW), "1d ago")

    def test_n_days(self):
        then = datetime(2026, 7, 8, 12, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(freshness.format_age(then, NOW), "14d ago")

    def test_partial_day_still_counts_whole_days_down(self):
        # 25h ago → 1 whole day (delta.days floors).
        then = datetime(2026, 7, 21, 11, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(freshness.format_age(then, NOW), "1d ago")
        # 23h ago → still same day bucket.
        then = datetime(2026, 7, 21, 13, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(freshness.format_age(then, NOW), "today")

    def test_future_stamp_clamps_to_today(self):
        then = datetime(2026, 7, 25, 12, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(freshness.format_age(then, NOW), "today")


class FormatStampTests(unittest.TestCase):
    def test_renders_date_and_age(self):
        self.assertEqual(
            freshness.format_stamp("2026-07-08T00:00:00Z", NOW),
            "2026-07-08 (14d ago)",
        )

    def test_missing_or_malformed_is_unknown(self):
        for bad in (None, "", "garbage"):
            with self.subTest(bad=bad):
                self.assertEqual(freshness.format_stamp(bad, NOW), "unknown")


class RenderTests(unittest.TestCase):
    def test_full_line(self):
        line = freshness.render(
            "2026-07-21T00:00:00Z", "2026-07-08T00:00:00Z", NOW
        )
        self.assertEqual(
            line,
            "container: up'd 2026-07-21 (1d ago) · image built 2026-07-08 (14d ago)",
        )

    def test_one_stamp_missing_shows_unknown_for_the_other(self):
        line = freshness.render("2026-07-21T00:00:00Z", None, NOW)
        self.assertEqual(
            line, "container: up'd 2026-07-21 (1d ago) · image built unknown"
        )

    def test_both_missing_yields_empty_string(self):
        self.assertEqual(freshness.render(None, None, NOW), "")
        self.assertEqual(freshness.render("", "garbage-is-not-a-stamp-x", NOW), "")


class ReadEnvFileTests(unittest.TestCase):
    def test_parses_key_values_and_ignores_noise(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "environment"
            p.write_text(
                'PATH="/usr/bin:/bin"\n'
                "# a comment\n"
                "\n"
                "DJINN_UP_AT=2026-07-21T00:00:00Z\n"
                "DJINN_IMAGE_BUILT=2026-07-08T00:00:00Z\n",
                encoding="utf-8",
            )
            values = freshness.read_env_file(str(p))
        self.assertEqual(values["PATH"], "/usr/bin:/bin")  # quotes stripped
        self.assertEqual(values["DJINN_UP_AT"], "2026-07-21T00:00:00Z")
        self.assertEqual(values["DJINN_IMAGE_BUILT"], "2026-07-08T00:00:00Z")

    def test_missing_file_is_empty_dict(self):
        self.assertEqual(freshness.read_env_file("/no/such/file/environment"), {})


class GetStampTests(unittest.TestCase):
    def setUp(self):
        # Ensure a clean slate for the env var under test.
        self._saved = os.environ.pop(freshness.UP_AT_VAR, None)

    def tearDown(self):
        if self._saved is not None:
            os.environ[freshness.UP_AT_VAR] = self._saved
        else:
            os.environ.pop(freshness.UP_AT_VAR, None)

    def test_env_var_wins_over_file(self):
        os.environ[freshness.UP_AT_VAR] = "from-env"
        self.assertEqual(
            freshness.get_stamp(freshness.UP_AT_VAR, {freshness.UP_AT_VAR: "from-file"}),
            "from-env",
        )

    def test_falls_back_to_file_when_env_absent(self):
        self.assertEqual(
            freshness.get_stamp(freshness.UP_AT_VAR, {freshness.UP_AT_VAR: "from-file"}),
            "from-file",
        )

    def test_none_when_neither_present(self):
        self.assertIsNone(freshness.get_stamp(freshness.UP_AT_VAR, {}))


class WiringTests(unittest.TestCase):
    """Guard the contract that feeds this helper: the Dockerfile must bake the
    script and source the hook, and up.sh must write the two stamps this reads.
    A drift here means the readout silently never renders."""

    def setUp(self):
        self.dockerfile = (REPO_ROOT / "Dockerfile").read_text()
        self.up_sh = (REPO_ROOT / "up.sh").read_text()
        self.hook = (REPO_ROOT / "src" / "freshness-landing.bashrc").read_text()

    def test_dockerfile_bakes_helper_at_the_path_the_hook_invokes(self):
        self.assertIn(
            "COPY src/freshness.py /usr/local/lib/djinn/freshness.py", self.dockerfile
        )
        self.assertIn("/usr/local/lib/djinn/freshness.py", self.hook)

    def test_dockerfile_sources_the_hook_before_tmux_landing(self):
        d = self.dockerfile
        self.assertIn(". /usr/local/share/freshness-landing.bashrc", d)
        self.assertLess(
            d.index("freshness-landing.bashrc"),
            d.index("tmux-landing.bashrc"),
            "freshness hook must be sourced before tmux-landing (which execs "
            "tmux and never returns), else it never renders in the tmux pane",
        )

    def test_up_sh_writes_both_stamps(self):
        for var in (freshness.UP_AT_VAR, freshness.IMAGE_BUILT_VAR):
            with self.subTest(var=var):
                self.assertIn(var, self.up_sh)
        # The image stamp must come from the image's real .Created, not the
        # container's — that is the whole point of the two-stamp split.
        self.assertIn("{{.Created}}", self.up_sh)

    def test_hook_is_interactive_only(self):
        self.assertIn("$- == *i*", self.hook)


if __name__ == "__main__":
    unittest.main()
