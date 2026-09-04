"""Pin which agent descriptors declare a global rules_file.

compose_rules.py writes the composed rules only to agents whose agent.yml
declares `rules_file`; an agent without one silently receives nothing. This
pins the current set so removing or mistyping a declaration fails CI, and
checks each declared path passes the manifest's home-relative validation.
"""
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
import manifest  # noqa: E402

EXPECTED = {
    "antigravity-cli": ".gemini/GEMINI.md",
    "claude": ".claude/CLAUDE.md",
    "codex": ".codex/AGENTS.md",
    "kimi": ".kimi-code/AGENTS.md",
    "pi": ".pi/agent/AGENTS.md",
}
RULES_LINE = re.compile(r"^rules_file:[ \t]*(.*?)[ \t]*(?:#.*)?$", re.M)


def declared_rules_files():
    """agent -> rules_file for every descriptor that declares a truthy one.

    Mirrors manifest.py's reading: a quoted scalar is unquoted, and a value
    manifest treats as falsy (null, false) or an empty scalar counts as undeclared, so
    the pin tracks what compose_rules will actually write."""
    found = {}
    for agent_yml in sorted((ROOT / "agents").glob("*/agent.yml")):
        m = RULES_LINE.search(agent_yml.read_text(encoding="utf-8"))
        if not m:
            continue
        value = m.group(1).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if value == "" or value in ("null", "~", "false") or manifest._falsy(value):
            continue
        found[agent_yml.parent.name] = value
    return found


class AgentRulesFileTest(unittest.TestCase):
    def test_declared_rules_files_match_expected_set(self):
        self.assertEqual(declared_rules_files(), EXPECTED)

    def test_pi_declares_its_agent_dir_context_file(self):
        self.assertEqual(declared_rules_files().get("pi"), ".pi/agent/AGENTS.md")

    def test_every_declared_path_is_home_relative_and_clean(self):
        for agent, path in declared_rules_files().items():
            with self.subTest(agent=agent):
                self.assertEqual(manifest._agent_home_path(agent, "rules_file", path), path)


if __name__ == "__main__":
    unittest.main()
