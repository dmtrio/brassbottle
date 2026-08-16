# freshness-landing.bashrc — sourced from ~/.bashrc, BEFORE the tmux-landing
# hook (which execs tmux and never returns for SSH/mosh logins). PLN - Container
# Freshness Readout: a passive, no-network, one-line readout of how old this
# container's config is — last `up` + image build date — so the human decides
# when to re-`up`. Zero runtime failure surface: no network, no cache, no auth.
#
# Scope guard: interactive shells only ($- has 'i'). Non-interactive channels
# (scp, VS Code Remote-SSH's command channel, `docker exec ... claude -p`) stay
# untouched. Unlike tmux-landing there is NO parent-process guard: this SHOULD
# print in attach-mode `docker exec` terminals too, not just sshd/mosh logins.
# src/freshness.py formats the relative age (host-clock, unit-tested) and prints
# nothing when no stamp is present — a readout, never an alarm.
if [[ $- == *i* ]]; then
    __freshness_line="$(python3 /usr/local/lib/djinn/freshness.py 2>/dev/null)"
    [ -n "$__freshness_line" ] && printf '\033[2m%s\033[0m\n' "$__freshness_line"
    unset __freshness_line
fi
