"""Guard the auth/state volume contract after agents-as-plugins Phase 1.

Static compose keeps only workspace + gh-auth. Agent auth/state volumes now come
from agents/*/agent.yml state_dirs via generated overlays, and Dockerfile auth
precreation must be descriptor-driven (not hardcoded paths).
"""

import re
import unittest
from pathlib import Path

REPO = Path(__file__).parent.parent
COMPOSE = REPO / "compose" / "docker-compose.local.yml"
DOCKERFILE = REPO / "Dockerfile"
AGENTS_DIR = REPO / "agents"

HOME = "/home/coder"
_VOL_LINE = re.compile(r"^\s*-\s*([A-Za-z0-9_-]+):(/home/coder/[^:\s]+)(:ro)?\s*$")
_STATE_DIR_LINE = re.compile(
    r"^\s*-\s*\{path:\s*([^,}\s]+)\s*,\s*volume:\s*([^,}\s]+)\s*\}\s*$",
    re.MULTILINE,
)


def compose_named_home_mounts():
    mounts = {}
    for line in COMPOSE.read_text(encoding="utf-8").splitlines():
        m = _VOL_LINE.match(line)
        if m and not m.group(3):  # writable named volumes only
            mounts[m.group(1)] = m.group(2)
    return mounts


def compose_top_level_volumes():
    names = set()
    in_volumes = False
    for line in COMPOSE.read_text(encoding="utf-8").splitlines():
        if not in_volumes:
            if line == "volumes:":
                in_volumes = True
            continue
        if line and not line.startswith(" "):
            break
        m = re.match(r"^\s{2}([A-Za-z0-9_-]+):\s*$", line)
        if m:
            names.add(m.group(1))
    return names


def agent_state_dirs():
    dirs = []
    for f in sorted(AGENTS_DIR.glob("*/agent.yml")):
        agent = f.parent.name
        text = f.read_text(encoding="utf-8")
        for path, volume in _STATE_DIR_LINE.findall(text):
            dirs.append((agent, path, volume))
    return dirs


class AuthVolumeDirTests(unittest.TestCase):
    def test_compose_static_volumes_are_workspace_and_gh_auth_only(self):
        self.assertEqual(compose_top_level_volumes(), {"workspace", "gh-auth"})

    def test_compose_keeps_only_static_home_named_mount(self):
        self.assertEqual(compose_named_home_mounts(), {"gh-auth": f"{HOME}/.config/gh"})

    def test_agent_state_dirs_are_descriptor_declared_not_static_compose(self):
        desc = agent_state_dirs()
        self.assertTrue(desc, "expected at least one agent state_dirs entry")
        static_mounts = compose_named_home_mounts()
        static_vol_names = set(static_mounts)
        static_targets = set(static_mounts.values())
        for agent, rel_path, volume in desc:
            with self.subTest(agent=agent, volume=volume, path=rel_path):
                target = f"{HOME}/{rel_path}"
                self.assertNotIn(
                    volume,
                    static_vol_names,
                    f"agent volume '{volume}' should come from generated agents overlay, not static compose",
                )
                self.assertNotIn(
                    target,
                    static_targets,
                    f"agent mount '{target}' should come from generated agents overlay, not static compose",
                )

    def test_dockerfile_precreate_loop_is_descriptor_driven(self):
        text = DOCKERFILE.read_text(encoding="utf-8")
        self.assertIn("for f in /opt/agents/*/agent.yml; do", text)
        self.assertIn("yq -r '.state_dirs[]?.path // \"\"' \"$f\"", text)
        self.assertIn("mkdir -p \"/home/$USERNAME/$rel\"", text)
        self.assertIn("mkdir -p /home/$USERNAME/.config/gh", text)


if __name__ == "__main__":
    unittest.main()
