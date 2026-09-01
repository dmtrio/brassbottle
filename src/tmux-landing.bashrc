# tmux-landing.bashrc — sourced at the END of ~/.bashrc (RFC 04).
# Interactive remote/editor terminals land in a FRESH tmux session per login,
# so each shell starts empty while still letting you jump into existing work.
#
# Scope guards, in order:
#   $REMOTE_SHELL  — tmux (default) lands; bash opts out (the entrypoint
#                    persists it to /etc/environment for PAM)
#   $TMUX          — shells inside tmux must not recurse
#   $- has i       — non-interactive channels (scp, VS Code Remote-SSH's
#                    command channel, tasks/debug/git shells) stay untouched
#   trigger source — sshd (ssh logins; 'sshd-session' since OpenSSH 9.8 split
#                    the per-session binary), mosh-server (mosh logins), OR
#                    TERM_PROGRAM=vscode (interactive VS Code/Cursor terminals —
#                    BOTH Remote-SSH and attach-to-running-container flows: the
#                    editor sets that var in every integrated terminal, so the
#                    attach flow lands in tmux too, by request).
#                    /proc/<pid>/comm is what `ps -o comm=` reads — used
#                    directly so the check needs no extra package.
#                    Plain docker exec shells (no TERM_PROGRAM) remain exempt:
#                    agents rely on bare shells there. ntfy notifications are
#                    per-window via tmux hook and unaffected by session names.
if [ "${REMOTE_SHELL:-tmux}" = "tmux" ] && [ -z "${TMUX:-}" ] && [[ $- == *i* ]]; then
    should_land=false
    case "$(cat "/proc/$PPID/comm" 2>/dev/null)" in
        sshd|sshd-session|mosh-server) should_land=true ;;
    esac
    [ "${TERM_PROGRAM:-}" = "vscode" ] && should_land=true

    if [ "$should_land" = "true" ]; then
        # Best-effort GC first: clean stale empty login-* sessions before
        # creating the next fresh one. Output (incl. tracebacks) goes to the
        # GC's own log — stdout here would print into the login banner.
        python3 /usr/local/lib/djinn/tmux_landing_gc.py \
            >>/tmp/djinn-tmux-landing-gc.log 2>&1 || true

        # '=' forces an exact-name match — a bare -t prefix-matches, so
        # login-42 would false-positive against a live login-421.
        name="login-$$"
        if tmux has-session -t "=$name" 2>/dev/null; then
            name="login-$$-$RANDOM"
        fi

        # -A on both paths: if the name collides after all (PID reuse racing
        # the has-session probe), attach instead of erroring — `exec tmux`
        # has already replaced this shell, so a failed new-session would
        # kill the login outright with no shell at all.
        if [ "$(tmux list-sessions 2>/dev/null | wc -l)" -gt 0 ]; then
            exec tmux new-session -A -s "$name" \; choose-tree -Zs
        else
            exec tmux new-session -A -s "$name"
        fi
    fi
    # Not landing (e.g. docker exec): don't leave scratch vars in the shell.
    unset should_land
fi
