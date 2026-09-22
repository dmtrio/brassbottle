#!/usr/bin/env python3
"""Tree-wide guard: the retired git-key spellings are gone.

The manifest's old git credential keys were rejected outright; every piece of
machinery that existed only to serve them (the owner-keyed routing tables, the
CLI-token default source, the per-owner attribution and notices) was deleted.
Nothing outside the rejection code and its own tests may mention those keys or
any symbol that existed only to read them — a surviving reference means a
reader of a removed output was missed. This suite turns that into a loud
failure instead of a silent half-deletion.
"""

import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# The rejection code itself, its tests, and this file: the only places a
# removed key or symbol may still be named.
ALLOWED_FILES = {
    "src/manifest.py",
    "tests/test_manifest.py",
    "tests/test_no_old_git_keys.py",
}

# The three retired manifest keys (spelled as they appear in a manifest or an
# error message) plus every derived variable, helper and notice that existed
# only to serve them.
BANNED_STRINGS = (
    # deleted derived keys
    "GIT_TOKEN_SOURCE",
    "GIT_ORG_TOKENS",
    "GIT_ORG_HOSTS",
    "GIT_ORG_IDENTITIES",
    "GIT_ORG_ROUTED_HOSTS",
    "FORGE",
    # deleted helpers (manifest.py, notices, skill pre-flight)
    "_canonical_token_var",
    "_org_hosts",
    "_check_token_routing",
    "_ssh_repo_owners",
    "check_org_host_binding",
    "git_orgs_host_notice",
    # the old GH_HOST_<owner> binding env vars
    "GH_HOST_",
    # the old manifest spellings themselves
    "git.orgs",
    "git.token",
    "forge:",
)


class TestNoOldGitKeyReferences(unittest.TestCase):
    def test_no_file_mentions_a_removed_key_or_symbol(self):
        offenders = []
        for path in sorted(REPO.rglob("*")):
            if not path.is_file():
                continue
            if "__pycache__" in path.parts or path.suffix == ".pyc":
                continue
            rel = path.relative_to(REPO).as_posix()
            if rel.startswith(".git/") or rel == ".git":
                continue
            if rel in ALLOWED_FILES:
                continue
            text = path.read_text(errors="replace")
            for needle in BANNED_STRINGS:
                if needle in text:
                    offenders.append(f"{rel}: {needle}")
        self.assertEqual(
            offenders, [],
            "references to removed git-key spellings or their machinery")


if __name__ == "__main__":
    unittest.main()
