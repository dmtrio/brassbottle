"""Unit tests for src/pull_manifests.py.

Real git repositories in temp dirs, not mocks: the whole value of this file is
that it correctly distinguishes "an external manifests repo" from "brassbottle
itself" and from "a plain directory", and every one of those distinctions is a
git question. A mocked `git` would test the mock.

The failure this guards against is silent in both directions — pulling
brassbottle when asked to pull manifests, or leaving a merged manifest unread —
so each case asserts on the printed line as well as the return value.
"""

import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import pull_manifests as pm  # noqa: E402


def git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True,
                   capture_output=True, text=True)


def init_repo(path):
    """A repo with one commit, committer identity set locally."""
    os.makedirs(path, exist_ok=True)
    git("init", "-q", "-b", "main", cwd=path)
    git("config", "user.email", "t@example", cwd=path)
    git("config", "user.name", "t", cwd=path)
    Path(path, "seed.yml").write_text("task: seed\n")
    git("add", "-A", cwd=path)
    git("commit", "-q", "-m", "seed", cwd=path)
    return path


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(_rmtree, self.tmp)

    def path(self, *parts):
        return os.path.join(self.tmp, *parts)

    def run_pull(self, *args, **kwargs):
        buf = io.StringIO()
        result = pm.pull(*args, out=buf, **kwargs)
        return result, buf.getvalue()


def _rmtree(path):
    import shutil
    shutil.rmtree(path, ignore_errors=True)


class Decide(Base):
    def test_bundled_is_never_pulled(self):
        should, reason = pm.decide(self.tmp, None, bundled=True)
        self.assertFalse(should)
        self.assertIn("bundled", reason)

    def test_plain_directory_is_not_pulled(self):
        plain = self.path("plain")
        os.makedirs(plain)
        should, reason = pm.decide(plain, None, bundled=False)
        self.assertFalse(should)
        self.assertIn("not a git checkout", reason)

    def test_missing_directory_is_not_pulled(self):
        should, _ = pm.decide(self.path("absent"), None, bundled=False)
        self.assertFalse(should)

    def test_bottles_dir_inside_brassbottle_is_not_pulled(self):
        # The exact hazard: BOTTLES_PATH pointed at this repo's own
        # bottles/, where a pull would pull brassbottle.
        repo = init_repo(self.path("brassbottle"))
        inside = os.path.join(repo, "bottles")
        os.makedirs(inside)
        should, reason = pm.decide(inside, repo, bundled=False)
        self.assertFalse(should)
        self.assertIn("would pull brassbottle", reason)

    def test_symlinked_self_is_still_recognised(self):
        # Path comparison alone misfires through a symlink; realpath saves it.
        repo = init_repo(self.path("brassbottle"))
        link = self.path("link-to-brassbottle")
        os.symlink(repo, link)
        should, _ = pm.decide(os.path.join(link, "bottles"), repo,
                              bundled=False)
        self.assertFalse(should)

    def test_external_repo_is_pulled(self):
        repo = init_repo(self.path("manifests"))
        should, _ = pm.decide(repo, init_repo(self.path("brassbottle")),
                              bundled=False)
        self.assertTrue(should)

    def test_subdirectory_of_an_external_repo_is_pulled(self):
        repo = init_repo(self.path("manifests"))
        sub = os.path.join(repo, "sub")
        os.makedirs(sub)
        should, _ = pm.decide(sub, None, bundled=False)
        self.assertTrue(should)


class RepoIdentity(Base):
    def test_a_linked_worktree_is_the_same_repository(self):
        # The layout this project's own workspace contract mandates: manifests
        # pointed at a brassbottle WORKTREE while --self is the main checkout.
        # --show-toplevel differs between them, so a toplevel comparison would
        # wave this through and pull a branch another agent has checked out.
        repo = init_repo(self.path("brassbottle"))
        wt = self.path("worktrees", "feature")
        git("worktree", "add", "-q", "-b", "feature", wt, cwd=repo)
        self.assertEqual(pm.repo_identity(wt), pm.repo_identity(repo))
        should, reason = pm.decide(wt, repo, bundled=False)
        self.assertFalse(should)
        self.assertIn("would pull brassbottle", reason)

    def test_bottles_dir_inside_a_worktree_is_also_refused(self):
        repo = init_repo(self.path("brassbottle"))
        wt = self.path("worktrees", "feature")
        git("worktree", "add", "-q", "-b", "feature", wt, cwd=repo)
        inside = os.path.join(wt, "bottles")
        os.makedirs(inside)
        should, _ = pm.decide(inside, repo, bundled=False)
        self.assertFalse(should)

    def test_an_unrelated_repo_keeps_its_own_identity(self):
        a = init_repo(self.path("manifests"))
        b = init_repo(self.path("brassbottle"))
        self.assertNotEqual(pm.repo_identity(a), pm.repo_identity(b))
        should, _ = pm.decide(a, b, bundled=False)
        self.assertTrue(should)

    def test_plain_directory_has_no_identity(self):
        plain = self.path("plain")
        os.makedirs(plain)
        self.assertIsNone(pm.repo_identity(plain))


class Hardening(Base):
    def test_git_runs_without_prompting(self):
        # An HTTPS credential prompt or unknown SSH host key would block up.sh
        # forever with its output captured — nothing on screen, no timeout.
        seen = {}
        real = pm.subprocess.run

        def spy(cmd, **kwargs):
            seen.update(kwargs.get("env") or {})
            seen["_timeout"] = kwargs.get("timeout")
            return real(cmd, **kwargs)

        pm.subprocess.run = spy
        self.addCleanup(setattr, pm.subprocess, "run", real)
        pm.git(["rev-parse", "HEAD"], cwd=init_repo(self.path("r")))
        self.assertEqual(seen.get("GIT_TERMINAL_PROMPT"), "0")
        self.assertIn("BatchMode=yes", seen.get("GIT_SSH_COMMAND", ""))

    def test_pull_passes_a_timeout(self):
        seen = {}
        real = pm.subprocess.run

        def spy(cmd, **kwargs):
            if "pull" in cmd:
                seen["timeout"] = kwargs.get("timeout")
            return real(cmd, **kwargs)

        pm.subprocess.run = spy
        self.addCleanup(setattr, pm.subprocess, "run", real)
        pm.pull(init_repo(self.path("solo")), out=io.StringIO())
        self.assertEqual(seen.get("timeout"), pm.PULL_TIMEOUT_SECONDS)

    def test_a_hanging_remote_is_reported_not_raised(self):
        real = pm.subprocess.run

        def spy(cmd, **kwargs):
            if "pull" in cmd:
                raise pm.subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))
            return real(cmd, **kwargs)

        pm.subprocess.run = spy
        self.addCleanup(setattr, pm.subprocess, "run", real)
        buf = io.StringIO()
        self.assertFalse(pm.pull(init_repo(self.path("solo")), out=buf))
        self.assertIn("timed out", buf.getvalue())


class Pull(Base):
    def clone_pair(self):
        """(upstream, clone) — a clone whose upstream has moved ahead."""
        upstream = init_repo(self.path("upstream"))
        clone = self.path("clone")
        subprocess.run(["git", "clone", "-q", upstream, clone], check=True,
                       capture_output=True)
        Path(upstream, "new-bottle.yml").write_text("task: new\n")
        git("add", "-A", cwd=upstream)
        git("commit", "-q", "-m", "add bottle", cwd=upstream)
        return upstream, clone

    def test_merged_manifest_arrives(self):
        _, clone = self.clone_pair()
        self.assertFalse(os.path.exists(os.path.join(clone, "new-bottle.yml")))
        pulled, output = self.run_pull(clone)
        self.assertTrue(pulled)
        self.assertTrue(os.path.exists(os.path.join(clone, "new-bottle.yml")))
        self.assertIn("fast-forwarded", output)

    def test_an_already_current_clone_says_so(self):
        # "already up to date" and "a merged bottle just landed" must not read
        # identically, or the line stops carrying information.
        _, clone = self.clone_pair()
        self.run_pull(clone)                       # first pull takes the commit
        pulled, output = self.run_pull(clone)      # second has nothing to take
        self.assertTrue(pulled)
        self.assertIn("already current", output)
        self.assertNotIn("fast-forwarded", output)

    def test_a_real_fast_forward_names_the_commits(self):
        _, clone = self.clone_pair()
        _, output = self.run_pull(clone)
        self.assertIn("fast-forwarded", output)
        self.assertIn("..", output)

    def test_bundled_flag_leaves_the_checkout_alone(self):
        _, clone = self.clone_pair()
        pulled, output = self.run_pull(clone, bundled=True)
        self.assertFalse(pulled)
        self.assertFalse(os.path.exists(os.path.join(clone, "new-bottle.yml")))
        self.assertIn("never pulled", output)

    def test_no_upstream_is_reported_not_fatal(self):
        repo = init_repo(self.path("solo"))       # no remote at all
        pulled, output = self.run_pull(repo)
        self.assertFalse(pulled)
        self.assertIn("could not fast-forward", output)

    def test_local_edit_survives_a_pull_that_does_not_touch_it(self):
        # git fast-forwards a dirty tree happily as long as the incoming commit
        # touches no edited file — so the merged bottle arrives AND the local
        # edit stays. Pinned because it is the common case: someone tweaking a
        # manifest by hand while an unrelated bottle lands upstream.
        _, clone = self.clone_pair()          # upstream adds new-bottle.yml
        Path(clone, "seed.yml").write_text("task: locally-edited\n")
        pulled, _ = self.run_pull(clone)
        self.assertTrue(pulled)
        self.assertTrue(os.path.exists(os.path.join(clone, "new-bottle.yml")))
        self.assertEqual(Path(clone, "seed.yml").read_text(),
                         "task: locally-edited\n")

    def test_local_edit_to_an_incoming_file_blocks_the_pull_not_the_run(self):
        # Here git refuses rather than clobber the edit. The manifest the user
        # is mid-edit must win, and up.sh must still proceed.
        upstream = init_repo(self.path("upstream2"))
        clone = self.path("clone2")
        subprocess.run(["git", "clone", "-q", upstream, clone], check=True,
                       capture_output=True)
        Path(upstream, "seed.yml").write_text("task: upstream-change\n")
        git("add", "-A", cwd=upstream)
        git("commit", "-q", "-m", "upstream edits seed", cwd=upstream)
        Path(clone, "seed.yml").write_text("task: locally-edited\n")
        pulled, output = self.run_pull(clone)
        self.assertFalse(pulled)
        self.assertEqual(Path(clone, "seed.yml").read_text(),
                         "task: locally-edited\n")
        self.assertIn("using it as-is", output)

    def test_diverged_history_is_not_merged(self):
        upstream, clone = self.clone_pair()
        Path(clone, "local-only.yml").write_text("task: local\n")
        git("add", "-A", cwd=clone)
        git("-c", "user.email=t@example", "-c", "user.name=t",
            "commit", "-q", "-m", "local", cwd=clone)
        pulled, output = self.run_pull(clone)
        self.assertFalse(pulled)      # --ff-only, so no merge commit appears
        self.assertIn("could not fast-forward", output)

    def test_plain_directory_says_so(self):
        plain = self.path("plain")
        os.makedirs(plain)
        pulled, output = self.run_pull(plain)
        self.assertFalse(pulled)
        self.assertIn("not a git checkout", output)

    def test_every_path_prints_exactly_one_line(self):
        # up.sh prints this inline with its other progress output; a silent or
        # multi-line result would make a stale manifest easy to miss.
        _, clone = self.clone_pair()
        for kwargs in ({}, {"bundled": True}):
            _, output = self.run_pull(clone, **kwargs)
            self.assertEqual(len(output.strip().splitlines()), 1, output)


class Cli(Base):
    def test_main_always_exits_zero(self):
        # up.sh runs under `set -e`; a non-zero exit here would abort bring-up
        # over a network hiccup.
        repo = init_repo(self.path("solo"))
        self.assertEqual(pm.main([repo]), 0)
        self.assertEqual(pm.main([self.path("absent")]), 0)
        self.assertEqual(pm.main([repo, "--bundled"]), 0)

    def test_invoked_as_a_subprocess(self):
        script = Path(__file__).resolve().parent.parent / "src" / "pull_manifests.py"
        repo = init_repo(self.path("solo"))
        p = subprocess.run([sys.executable, str(script), repo],
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 0)
        self.assertIn("manifests:", p.stdout)


if __name__ == "__main__":
    unittest.main()
