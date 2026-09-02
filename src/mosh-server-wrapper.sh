#!/bin/bash
# mosh-server wrapper — jump-only (the jump carries mosh for the whole
# fleet: mosh coder@<jump ip>, then ssh djinn-<bottle>; bottles no longer
# run mosh-server). Installed as /usr/local/bin/mosh-server, which precedes
# /usr/bin on PATH, so the `mosh-server new ...` command that the mosh
# client launches over SSH resolves here. Pins the server to the UDP range
# the jump's firewall accepts and its compose publishes; a client cannot
# land on an unreachable port.
#
# The pin must be spliced BEFORE the first `--`: with `mosh host -- cmd`
# the client sends `mosh-server new [opts] -- cmd`, and getopt stops at
# `--`, so an appended -p would become argv of the remote command and the
# pin silently dropped. Placed there it also wins over any client-sent -p
# (mosh-server takes the last occurrence). Non-`new` invocations use the
# legacy positional syntax, which takes no options — pass through untouched.
if [ "${1:-}" = "new" ]; then
    shift
    PIN=(-p "${MOSH_PORTS:-60000:60010}")
    ARGS=()
    for a in "$@"; do
        if [ "$a" = "--" ] && [ ${#PIN[@]} -gt 0 ]; then
            ARGS+=("${PIN[@]}")
            PIN=()
        fi
        ARGS+=("$a")
    done
    if [ ${#PIN[@]} -gt 0 ]; then
        ARGS+=("${PIN[@]}")
    fi
    exec /usr/bin/mosh-server new "${ARGS[@]}"
fi
exec /usr/bin/mosh-server "$@"
