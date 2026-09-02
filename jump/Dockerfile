# djinn-jump — singleton mosh jump container (PLN - Djinn Admin Plane, PR 1).
#
# Terminates the operator's inbound mosh session and hops onward to bottles
# over djinn-net. Deliberately minimal: sshd + mosh and nothing else. No tmux
# — the durable session still lives in the BOTTLE (src/tmux-landing.bashrc),
# and the jump only has to survive long enough to carry the hop.
#
# Debian rather than Alpine: mosh-server aborts without a UTF-8 native locale,
# and the locale incantation below is the one already proven in the bottle
# image (RFC 04 A1 + the PR #7 review fix that added update-locale).
FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        openssh-server mosh locales ca-certificates python3 \
    && rm -rf /var/lib/apt/lists/* \
    && sed -i 's/^# *\(en_US.UTF-8\)/\1/' /etc/locale.gen \
    && locale-gen en_US.UTF-8 \
    && update-locale LANG=en_US.UTF-8
ENV LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8

# Same wrapper the bottles use: pins client-launched mosh servers into the
# range we document, splicing -p before any `--`. Reused verbatim rather than
# reimplemented so the `mosh host -- cmd` argv bug stays fixed in one place.
COPY src/mosh-server-wrapper.sh /usr/local/bin/mosh-server
RUN chmod +x /usr/local/bin/mosh-server

RUN useradd -m -s /bin/bash -u 1000 coder \
    && mkdir -p /home/coder/.ssh /run/sshd \
    && chmod 700 /home/coder/.ssh \
    && chown -R coder:coder /home/coder/.ssh

COPY jump/entrypoint.sh /usr/local/bin/jump-entrypoint.sh
COPY jump/picker.py /usr/local/bin/djinn-jump-picker
COPY jump/ssh_config /etc/ssh/ssh_config.d/djinn-jump.conf
RUN chmod +x /usr/local/bin/jump-entrypoint.sh /usr/local/bin/djinn-jump-picker

ENTRYPOINT ["/usr/local/bin/jump-entrypoint.sh"]
