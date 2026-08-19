#!/usr/bin/env python3
"""pull_manifests.py — fast-forward the manifests checkout before up.sh reads it.

up.sh already keeps an EXTERNAL rules repo current (`git -C "$RULES_PATH" pull
--ff-only`), because merged rule PRs have to reach the container somehow. Once
manifests moved out of this repo into their own — the CONTAINERS_PATH override
in ./.env, e.g. ~/git/djinn-bottles — they acquired exactly the same problem
and none of the same treatment: a merged manifest PR sat unread until someone
remembered to pull by hand, and `./djinn up` cheerfully applied the stale file.

Two things must never be pulled:

  * The BUNDLED containers/ dir, which lives inside brassbottle itself — a pull
    there pulls brassbottle. up.sh's rules code guards this with a flag set
    where the fallback is chosen rather than a path comparison, because a
    symlinked path makes comparison misfire; CONTAINERS_BUNDLED is the same
    idea. The toplevel check below additionally catches an explicit
    CONTAINERS_PATH aimed back at this repo's own containers/.
  * Anything that isn't a git checkout at all — the plain-directory setup stays
    supported and must not start erroring.

Never fatal: a laptop offline, a detached HEAD, an unpushed local edit to a
manifest are all ordinary, and none is a reason to refuse to bring a container
up. Every outcome prints one line saying which it was, so a stale manifest can
never be mistaken for a fresh one.

Exit status is always 0.
"""

import argparse
import os
import subprocess
import sys


def git(args, cwd=None):
    """(returncode, stdout, stderr) — never raises."""
    try:
        p = subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                           text=True)
    except (OSError, ValueError) as e:      # git absent, bad cwd
        return 1, "", str(e)
    return p.returncode, p.stdout.strip(), p.stderr.strip()


def toplevel(path):
    """The work-tree root containing path, or None when it isn't a checkout."""
    if not os.path.isdir(path):
        return None
    rc, out, _ = git(["rev-parse", "--show-toplevel"], cwd=path)
    return out or None if rc == 0 else None


def decide(containers_path, self_root, bundled):
    """(should_pull, reason). Pure: no git writes, so it is cheap to test."""
    if bundled:
        return False, "bundled containers/ (inside this repo) — never pulled"
    top = toplevel(containers_path)
    if top is None:
        return False, "not a git checkout — nothing to pull"
    self_top = toplevel(self_root) if self_root else None
    if self_top and os.path.realpath(top) == os.path.realpath(self_top):
        return False, "same checkout as brassbottle — a pull here would pull brassbottle"
    return True, top


def pull(containers_path, self_root=None, bundled=False, out=sys.stdout):
    """Fast-forward when it is safe to. Returns True when a pull ran and won."""
    should, reason = decide(containers_path, self_root, bundled)
    if not should:
        print("  manifests: %s" % reason, file=out)
        return False
    rc, _, err = git(["pull", "--ff-only", "-q"], cwd=containers_path)
    if rc == 0:
        print("  manifests: fast-forwarded %s" % reason, file=out)
        return True
    # Offline, no upstream, dirty tree, diverged history — all ordinary, none
    # worth blocking on. Say so rather than continuing in silence: the whole
    # point of this file is that nobody should have to wonder whether the
    # manifest about to be applied is the merged one.
    detail = " ".join(err.split())[:160] or "git pull exited %d" % rc
    print("  manifests: could not fast-forward %s — using it as-is (%s)"
          % (reason, detail), file=out)
    return False


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("containers_path")
    ap.add_argument("--self", dest="self_root", default=None,
                    help="brassbottle checkout, so we never pull ourselves")
    ap.add_argument("--bundled", action="store_true",
                    help="CONTAINERS_PATH is this repo's own containers/")
    args = ap.parse_args(argv)
    pull(args.containers_path, args.self_root, args.bundled)
    return 0        # never block bring-up


if __name__ == "__main__":
    sys.exit(main())
