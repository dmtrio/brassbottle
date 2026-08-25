#!/usr/bin/env python3
"""In-container startup for the `services:` plugin.yml key (Phase 1
Hardening PLN, workstream 2, PR [1/3]).

up.sh derives PLUGIN_SERVICES from src/manifest.py — one "name<TAB>command
<TAB>plugin" line per declared service of an ENABLED plugin — and, per
service, pipes this module's stdout into `docker exec -i -u coder
<container> bash`: the same stdin-script idiom src/wire_plugins.py already
uses for --build-payload. That is deliberate, not incidental: splicing a
plugin-authored command through three layers of shell quoting (up.sh's own
`bash -c "..."`, then `docker exec`, then `tmux new-session`) is exactly the
"set -e scope, quoting layers, silent empty-input behavior" the working
agreement's bash-vs-Python rule warns about. Generating one flat,
heredoc-written script file here — in Python, where string formatting does
not require three nested escaping passes — sidesteps it entirely, and it is
covered by tests/test_plugin_services.py instead of being untestable inline
bash.

Log path: /tmp/djinn-services/<name>.log, INSIDE the container. Deliberately
generic — this module has no idea what a particular plugin's service is or
where its author wants durable logs (a collabrain-style plugin that wants a
durable log declares its own `volumes:` and points its command at a path
under that mount; this module just guarantees SOME restart/crash history
exists per service, for the container's current lifetime). /tmp does not
survive a container recreate, which is fine for that purpose.

Idempotent by construction: `tmux has-session -t svc-<name>` gates the whole
body, so re-running the generated script (i.e. re-running `./djinn up`) is a
no-op for a service still alive and a fresh start for one that died —
exactly the "restarts only what died" behavior up.sh's plugin-services
section relies on.
"""

import re
import sys

LOG_DIR = "/tmp/djinn-services"
SERVICE_NAME_RE = re.compile(r"^[a-z0-9-]+\Z")

# "Exponential-ish" backoff, not true exponential: an exit sooner than
# FAST_EXIT_SECS after start counts as a crash-loop signal, and two of those
# in a row escalate the sleep from BACKOFF_FIRST to BACKOFF_ESCALATED. A slow
# (non-crash-loop) exit resets the counter. Nothing caps retries — a service
# is expected to keep trying forever, and every attempt is logged, so a
# spinning service is loud (a growing log), never silent.
FAST_EXIT_SECS = 60
BACKOFF_FIRST = 5
BACKOFF_ESCALATED = 30


def wrapper_script(name, command):
    """The full, self-contained bash script for ONE service: an idempotent
    tmux guard, a restart-wrapper written to a small script file under
    LOG_DIR, then launched detached as tmux session `svc-<name>`.

    Feed the result to `docker exec -i -u coder <container> bash` on stdin
    (see module docstring for why not `bash -c "<script>"`)."""
    if not isinstance(name, str) or not SERVICE_NAME_RE.match(name):
        raise ValueError(
            f"service name {name!r} is not kebab-case (lowercase letters, digits, dash only)")
    if not isinstance(command, str) or not command.strip():
        raise ValueError(f"service '{name}': command must be a non-empty string")

    session = f"svc-{name}"
    log_path = f"{LOG_DIR}/{name}.log"
    script_path = f"{LOG_DIR}/{name}.sh"
    # Unique per service and (barring a name containing this exact literal,
    # which SERVICE_NAME_RE already rules out having odd chars in) not a
    # substring the command could plausibly emit — a plain 'EOF' is common
    # enough in real command output to risk truncating the heredoc early.
    heredoc_tag = "DJINN_SVC_" + name.upper().replace("-", "_") + "_EOF"

    return f"""set -u
mkdir -p {LOG_DIR}
if tmux has-session -t {session} 2>/dev/null; then
  echo "  = {session} already running"
else
  cat > {script_path} <<'{heredoc_tag}'
log() {{ printf '%s %s\\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >> {log_path}; }}
fails=0
while true; do
  start_ts=$(date +%s)
  log "start (attempt $((fails + 1)))"
  {command}
  code=$?
  dur=$(( $(date +%s) - start_ts ))
  log "exit code=$code after ${{dur}}s"
  if [ "$dur" -lt {FAST_EXIT_SECS} ]; then
    fails=$((fails + 1))
  else
    fails=0
  fi
  if [ "$fails" -ge 2 ]; then
    sleep {BACKOFF_ESCALATED}
  else
    sleep {BACKOFF_FIRST}
  fi
done
{heredoc_tag}
  chmod +x {script_path}
  if tmux new-session -d -s {session} bash {script_path}; then
    echo "  + {session} started"
  else
    echo "  ! {session} FAILED to start (tmux new-session failed)" >&2
    exit 1
  fi
fi
"""


def main(argv):
    if len(argv) != 2:
        print("Error: usage: plugin_services.py <name> <command>", file=sys.stderr)
        return 2
    name, command = argv
    try:
        sys.stdout.write(wrapper_script(name, command))
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
