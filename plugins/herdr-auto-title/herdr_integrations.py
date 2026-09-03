#!/usr/bin/env python3
"""Install herdr's agent-state hook for every agent this bottle enables.

The plugin's `setup:` command. herdr ships one hook per agent
(`herdr integration install <target>`), and the hook is what tells herdr —
and so Auto Title — which session a pane holds and whether the agent is
working, blocked or done. Hooks land in each agent's state directory
(~/.claude, ~/.codex, …), which are volumes the image cannot pre-populate, so
this runs in the live container at `./djinn up`, once per enabled agent.

Which agents are enabled comes from /usr/local/lib/djinn/agents-index.tsv,
rendered at image build from the manifest's agents list (first column is the
agent directory name under agents/). brassbottle's agent names coincide with
herdr's integration targets where herdr has one; an enabled agent herdr has
no hook for (aider) is skipped with a log line, never an error.

Idempotent: upstream re-installs the hook in place and reports it. Each
install is its own subprocess with its own exit code, so one agent's failure
does not stop the others — the exit status is non-zero if ANY failed, which
the setup: wrapper turns into one loud line on the `up` console.

Boundary logging (stderr → the setup: log): the index read (path, agents
found), every herdr call (target, duration, exit code, stdout/stderr size),
the skips, and a final summary.
"""

import os
import subprocess
import sys
import time

INDEX_PATH = "/usr/local/lib/djinn/agents-index.tsv"
# herdr 0.8.2 `integration install` targets. brassbottle agent names map 1:1
# where an entry exists; anything else is skipped. Kept as data so a new herdr
# target or a new agent is a one-line change with a test, not a code path.
HERDR_TARGETS = frozenset({
    "pi", "omp", "claude", "codex", "copilot", "devin", "droid", "kimi",
    "opencode", "kilo", "hermes", "qodercli", "qwen", "cursor", "mastracode",
    "antigravity-cli", "grok",
})
# Deadlines. The setup: wrapper (src/plugin_setup.py, TIMEOUT_SECS) kills this
# whole process at 300 s, and a kill from outside means no per-agent line for
# the rest and no summary — the one silent path. So the TOTAL budget here is
# fixed below that ceiling, and each install gets the smaller of its own cap
# and an even share of what is left, so however many agents hang, every agent
# is accounted for and the summary always prints. Upstream's install is local
# file work (measured 0.7 s for six agents); the caps only bound a hang.
TOTAL_BUDGET_SECS = 240      # < plugin_setup.TIMEOUT_SECS (300) with headroom
PER_INSTALL_TIMEOUT_SECS = 30
MIN_INSTALL_TIMEOUT_SECS = 1


def log(stage, **fields):
    kv = " ".join(f"{k}={v}" for k, v in fields.items())
    print(f"herdr-integrations stage={stage} {kv}".rstrip(), file=sys.stderr, flush=True)


def read_enabled_agents(index_path):
    """Agent names from the index (first TSV column), in file order, deduped.
    A missing or empty index is reported and yields no agents — there is
    nothing to install, which is not a failure."""
    try:
        with open(index_path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except FileNotFoundError:
        log("index_read", status="missing", path=index_path)
        return []
    agents, seen = [], set()
    for line in lines:
        name = line.split("\t", 1)[0].strip()
        if name and name not in seen:
            seen.add(name)
            agents.append(name)
    log("index_read", status="ok", path=index_path, size_in=len(lines), agents=len(agents))
    return agents


def per_install_timeout(remaining_secs, agents_left):
    """The cap for the next install: its own cap, or an even share of the
    remaining budget when that is smaller. Never below MIN_INSTALL_TIMEOUT_SECS
    so a nearly spent budget still runs (and logs) rather than divides to 0."""
    share = remaining_secs / max(agents_left, 1)
    return max(MIN_INSTALL_TIMEOUT_SECS, min(PER_INSTALL_TIMEOUT_SECS, share))


def install_one(target, herdr="herdr", timeout=PER_INSTALL_TIMEOUT_SECS):
    """Run `herdr integration install <target>`; return its exit code.
    stdin is /dev/null (nothing here is interactive, and the setup wrapper's
    stdin is the wrapper script itself); a hang is a failure, not a stall."""
    start = time.monotonic()
    try:
        proc = subprocess.run(
            [herdr, "integration", "install", target],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout,
        )
        code, out, err = proc.returncode, proc.stdout, proc.stderr
    except OSError as e:
        # Not found, not executable, … — anything the exec itself refuses.
        # Caught per agent so the others still run and the summary still
        # prints; a traceback here would be the one silent path.
        log("install", target=target, status="error", reason=f"{herdr}: {e.strerror or e}")
        return 127
    except subprocess.TimeoutExpired:
        log("install", target=target, status="timeout", after_s=round(timeout, 1))
        return 124
    dur_ms = int((time.monotonic() - start) * 1000)
    log("install", target=target, status="ok" if code == 0 else "failed",
        code=code, duration_ms=dur_ms, stdout_bytes=len(out), stderr_bytes=len(err))
    # herdr's own lines (which file it wrote) belong in the setup log too.
    for stream in (out, err):
        if stream.strip():
            print(stream.rstrip(), file=sys.stderr, flush=True)
    return code


def main(argv):
    index_path = argv[0] if argv else os.environ.get("HERDR_INTEGRATIONS_INDEX", INDEX_PATH)
    herdr = os.environ.get("HERDR_INTEGRATIONS_BIN", "herdr")
    budget = float(os.environ.get("HERDR_INTEGRATIONS_BUDGET_SECS", TOTAL_BUDGET_SECS))
    agents = read_enabled_agents(index_path)
    targets = [a for a in agents if a in HERDR_TARGETS]
    installed, skipped, failed = [], [], []
    for agent in agents:
        if agent not in HERDR_TARGETS:
            log("skip", agent=agent, reason="no herdr integration target")
            skipped.append(agent)
    deadline = time.monotonic() + budget
    for i, agent in enumerate(targets):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            # Budget spent by earlier hangs: still one line per agent, and
            # the summary below still prints — nothing dies mid-loop.
            log("install", target=agent, status="skipped", reason="budget exhausted",
                budget_s=budget)
            failed.append(agent)
            continue
        timeout = per_install_timeout(remaining, len(targets) - i)
        (installed if install_one(agent, herdr, timeout) == 0 else failed).append(agent)
    log("summary", installed=",".join(installed) or "-", skipped=",".join(skipped) or "-",
        failed=",".join(failed) or "-", elapsed_s=round(budget - (deadline - time.monotonic()), 1))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
