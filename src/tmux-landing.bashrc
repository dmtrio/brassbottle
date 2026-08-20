# tmux-landing.bashrc — sourced at the END of ~/.bashrc (RFC 04).
# Interactive SSH and mosh logins land attached to ONE durable, shared tmux
# session (`new-session -A`: create or attach) — phone and laptop see the
# same view, and whatever runs there survives disconnects.
#
# Scope guards, in order:
#   $REMOTE_TMUX   — only containers whose bottle sets remote.tmux (the
#                    entrypoint persists it to /etc/environment for PAM)
#   $TMUX          — shells inside tmux must not recurse
#   $- has i       — non-interactive channels (scp, VS Code Remote-SSH's
#                    command channel) stay untouched
#   parent process — only sshd (ssh logins; 'sshd-session' since OpenSSH
#                    9.8 split the per-session binary) and mosh-server
#                    (mosh logins); docker exec (PPID outside the pid
#                    namespace) and editor-spawned terminals (parent: node)
#                    are exempt, so attach-mode workflows are unchanged.
#                    /proc/<pid>/comm is what `ps -o comm=` reads — used
#                    directly so the check needs no extra package.
if [ "${REMOTE_TMUX:-false}" = "true" ] && [ -z "${TMUX:-}" ] && [[ $- == *i* ]]; then
    case "$(cat "/proc/$PPID/comm" 2>/dev/null)" in
        sshd|sshd-session|mosh-server) exec tmux new-session -A -s agent ;;
    esac
fi
