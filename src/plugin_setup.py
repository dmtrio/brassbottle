#!/usr/bin/env python3
"""In-container one-shot setup for the `setup:` plugin.yml key (PLN "plugin
setup hook" [2/3]).

up.sh derives PLUGIN_SETUP from src/manifest.py — one "plugin<TAB>command"
line per ENABLED plugin that declares a setup command — and, per plugin,
pipes this module's stdout into `docker exec -i -u coder <container> bash`:
the same stdin-script idiom src/plugin_services.py already uses for
services: (see that module's docstring for why a heredoc-written script
beats splicing a plugin-authored command through three quoting layers).

Unlike services:, there is no tmux session and no restart loop: the command
runs once, synchronously, in the foreground — `up` waits for it. Re-runs on
every `./djinn up`, so a setup command MUST be idempotent (the docs say so;
e.g. `herdr integration install claude` reports `current` instead of
rewriting). It runs after the firewall is up, so it wires what the plugin's
`install:` block already fetched at image build and never downloads.

Log path: /tmp/djinn-setup/<plugin>.log, INSIDE the container, with start/
end timestamps, duration and exit code appended per run — /tmp does not
survive a container recreate, which is fine for a "what did the last setup
do" history. The command's own output is appended to the same log, so the
`up` console carries only one line per plugin.

A failed setup exits with the command's exit code and is non-fatal to `up`
(up.sh's `|| echo` posture, the same as a failed service start) — but loud.
"""

import re
import sys

LOG_DIR = "/tmp/djinn-setup"
# The plugin name is the directory name under plugins/ (up.sh derives it from
# the path), so this is the plugin-dir charset: it becomes a filename under
# LOG_DIR and the heredoc tag suffix — the same boring-charset reasoning as
# plugin_services.SERVICE_NAME_RE.
PLUGIN_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+\Z")


def setup_script(plugin, command):
    """The full, self-contained bash script for ONE plugin's setup command:
    write the command to a script file under LOG_DIR, run it with output
    appended to the plugin's log (timestamps, duration, exit code), echo one
    summary line, and exit with the command's exit code.

    Feed the result to `docker exec -i -u coder <container> bash` on stdin
    (see module docstring for why not `bash -c "<script>"`)."""
    if not isinstance(plugin, str) or not PLUGIN_NAME_RE.match(plugin):
        raise ValueError(
            f"plugin name {plugin!r} is not a plugin dir name "
            "(letters, digits, dash, underscore only)")
    if not isinstance(command, str) or not command.strip():
        raise ValueError(f"plugin '{plugin}': setup command must be a non-empty string")

    log_path = f"{LOG_DIR}/{plugin}.log"
    cmd_path = f"{LOG_DIR}/{plugin}.cmd.sh"
    # Unique per plugin and (barring a name containing this exact literal,
    # which PLUGIN_NAME_RE already rules out) not a substring the command
    # could plausibly emit — same reasoning as plugin_services.py's tag.
    heredoc_tag = "DJINN_SETUP_" + plugin.upper().replace("-", "_") + "_EOF"

    return f"""set -u
mkdir -p {LOG_DIR}
log() {{ printf '%s %s\\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >> {log_path}; }}
cat > {cmd_path} <<'{heredoc_tag}'
{command}
{heredoc_tag}
start_ts=$(date +%s)
log "start"
# Command output joins the log (start/end stamps, duration, exit code in one
# place); the up console only ever sees the summary line below.
bash {cmd_path} >> {log_path} 2>&1
code=$?
dur=$(( $(date +%s) - start_ts ))
log "exit code=$code after ${{dur}}s"
if [ "$code" -eq 0 ]; then
  echo "  + setup {plugin} ok (${{dur}}s)"
else
  echo "  ! setup {plugin} FAILED code=$code — see {log_path}" >&2
fi
exit "$code"
"""


def main(argv):
    if len(argv) != 2:
        print("Error: usage: plugin_setup.py <plugin> <command>", file=sys.stderr)
        return 2
    plugin, command = argv
    try:
        sys.stdout.write(setup_script(plugin, command))
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
