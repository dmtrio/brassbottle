# rules-compose.bashrc — sourced from ~/.bashrc on interactive shells.
# Recompose each agent's global rules file (base rules from the read-only
# /agent-rules mount + the AGENTS.md fragments of the plugins this container
# enabled) so host-side edits to the base show up without waiting for the next
# `up`. up.sh wrote the enabled-plugin list; compose_rules.py reads it.
#
# Guards: interactive only ($- has i) — scp / VS Code command channels stay
# untouched; best-effort (|| true) and silent — a compose hiccup must never
# break or slow-noise the shell. Sourced BEFORE the tmux-landing hook (which
# execs tmux and never returns), so it runs in the login shell too.
if [[ $- == *i* ]]; then
    python3 /usr/local/lib/djinn/compose_rules.py >/dev/null 2>&1 || true
fi
