# user-keys-landing.bashrc — per-identity git tokens for the human.
# Sourced by .bashrc (the Dockerfile appends the line just before the landing
# hooks that end the file). up.sh writes ~/.agent-keys/user.env — the `user`
# identity's own git routing table (GIT_HOST_TOKENS rows + the token
# variables those rows name + GH_TOKEN when its github.com row exists) —
# beside the agents' key files, mode 600 on a read-only mount. Sourcing it
# with set -a exports those variables so interactive git and gh act as the
# user identity, exactly like an agent's shim does for it.
#
# Only interactive shells ever reach this: Ubuntu's own non-interactive
# guard at the top of .bashrc (`case $- in *i*)... return`) returns long
# before the appended pieces, so `bash -c` sources nothing.
if [ -f "$HOME/.agent-keys/user.env" ]; then
    set -a
    . "$HOME/.agent-keys/user.env"
    set +a
fi
