#!/bin/bash
# tmux-notify.sh — idle notifier for remote.shell: tmux (RFC 04 Phase B).
# Fired by tmux's alert-silence hook (armed in tmux.conf only when NTFY_URL
# is present): the pane produced no output for the monitor-silence window,
# which for an agent means "waiting at a prompt" or "finished". Deliberately
# knows nothing about WHICH agent runs in the pane — works for all of them.
# For herdr shells, use herdr_notify.py instead (event-driven).
#
# Suppression: if any client is attached you are already looking at the
# session — pushing would self-notify on every pause. Only push when nobody
# is watching.
#
# $1: the window that went silent (tmux.conf passes #{hook_window}) — the
# alert-silence hook is server-global, so without it we'd capture whatever
# pane happens to front, not the one that idled. Required — there is no
# durable session name to fall back to.

[ -n "${NTFY_URL:-}" ] || exit 0

if [ "$(tmux list-clients 2>/dev/null | wc -l)" -gt 0 ]; then
    exit 0
fi

# No fallback target: the shared 'agent' session is gone (fresh-per-login
# landing), and a guessed target would capture the wrong pane. The tmux.conf
# hook always passes #{hook_window}; a manual invocation must too.
TARGET="${1:-}"
[ -n "$TARGET" ] || exit 0

# Last non-blank pane lines give the push its context (the prompt/question).
TAIL=$(tmux capture-pane -p -t "$TARGET" 2>/dev/null | grep -v '^[[:space:]]*$' | tail -n 3)

curl -s --max-time 10 \
    -H "Title: djinn-${CONTAINER_NAME:-container}: agent idle" \
    -H "Tags: robot" \
    -d "${TAIL:-<no recent output>}" \
    "${NTFY_URL%/}/${NTFY_TOPIC:-djinn-agents}" >/dev/null || true

exit 0
