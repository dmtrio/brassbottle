FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=America/Chicago

# ── Build args ────────────────────────────────────────────────────────────────
ARG USERNAME=coder
ARG USER_UID=1000
ARG USER_GID=1000
# Fail-closed default for direct `docker build .`; up.sh always passes the
# manifest-derived set explicitly.
ARG AGENTS_ENABLED=""
ARG PLUGINS_ENABLED=""

# ── System packages ───────────────────────────────────────────────────────────
RUN apt-get update && apt-get install -y \
    # Core utilities
    curl wget git git-lfs sudo \
    # Build tools
    build-essential pkg-config \
    # Python
    python3 python3-pip python3-venv \
    # Search / file tools
    ripgrep fd-find jq unzip zip \
    # GitHub CLI deps
    ca-certificates gnupg \
    && rm -rf /var/lib/apt/lists/*

# ── Python packages ──────────────────────────────────────────────────────────
# --break-system-packages: Ubuntu 24.04 marks Python as externally managed
# (PEP 668). Safe to override here — the container is the isolation layer.
RUN pip3 install pipenv playwright --break-system-packages \
    && pip3 cache purge

# ── GitHub CLI ────────────────────────────────────────────────────────────────
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y gh \
    && rm -rf /var/lib/apt/lists/*

# ── Gitea CLI (tea) ──────────────────────────────────────────────────────────
RUN curl -fsSL "https://dl.gitea.com/tea/0.9.2/tea-0.9.2-linux-$(dpkg --print-architecture)" -o /usr/local/bin/tea \
    && chmod +x /usr/local/bin/tea

# ── Create non-root user ──────────────────────────────────────────────────────
RUN userdel -r ubuntu 2>/dev/null || true \
    && groupadd --gid $USER_GID $USERNAME 2>/dev/null || true \
    && useradd --uid $USER_UID --gid $USER_GID -m -s /bin/bash $USERNAME \
    && echo "$USERNAME ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers

# ── User .ssh dir (authorized_keys injected at runtime by the entrypoint) ────
RUN mkdir -p /home/$USERNAME/.ssh \
    && chmod 700 /home/$USERNAME/.ssh \
    && chown -R $USERNAME:$USERNAME /home/$USERNAME/.ssh

# ── fnm (Fast Node Manager) ───────────────────────────────────────────────────
USER $USERNAME

RUN curl -fsSL https://fnm.vercel.app/install | bash -s -- --install-dir /home/$USERNAME/.fnm

# Add fnm to bash profile so it works in interactive AND non-interactive shells
RUN echo '' >> /home/$USERNAME/.bashrc \
    && echo '# fnm' >> /home/$USERNAME/.bashrc \
    && echo 'export PATH="/home/$USERNAME/.fnm:$PATH"' >> /home/$USERNAME/.bashrc \
    && echo 'eval "$(fnm env --use-on-cd --shell bash)"' >> /home/$USERNAME/.bashrc \
    # `fnm env` prepends fnm_multishells/<pid>/bin (the active node's global
    # bin, holding the REAL agent CLIs) to the FRONT of PATH in interactive
    # shells — ahead of the image's ENV PATH. Re-assert the shims here, AFTER
    # the fnm eval, so `claude`/`codex`/etc. launched from a terminal still
    # resolve to the identity shim (which loads per-agent MCP keys) and not
    # the bare binary. Without this, agents come up with no MCP credentials.
    && echo '# agent-shims must outrank fnm-injected node bin (see Dockerfile)' >> /home/$USERNAME/.bashrc \
    && echo 'export PATH="$HOME/.agent-shims:$PATH"' >> /home/$USERNAME/.bashrc \
    # Also add to .bash_profile for SSH login shells
    && echo '' >> /home/$USERNAME/.bash_profile \
    && echo 'source ~/.bashrc' >> /home/$USERNAME/.bash_profile

# Install a default LTS node (projects can override via .node-version)
ENV PATH="/home/coder/.local/bin:/home/coder/.fnm:$PATH"
RUN eval "$(fnm env)" && fnm install --lts && fnm use lts-latest

# ── uv (Python package manager) ──────────────────────────────────────────────
RUN curl -LsSf https://astral.sh/uv/install.sh | sh \
    && uv cache clean

# ── Plugins (drop-in local MCP tools) ────────────────────────────────────────
# Each plugin is a directory: plugins/<name>/plugin.yml (+ optional host-only
# run.sh). Every plugin.yml is baked into the shared image here. A LOCAL (stdio)
# plugin carries an `install:` block that runs at build time (full network) so
# the binary is present offline behind the runtime egress firewall. A REMOTE
# plugin (a bare `url:` spec, no binary) has no install: and is skipped here;
# nothing is baked, it's pure config wired by up.sh. Which plugins fall in
# which camp is not fixed — a remote HTTP service is often better reached via
# a LOCAL `mcp-remote` stdio bridge, because a remote `url:` spec only reaches
# Claude while a local one reaches every agent (see plugins/axiom, browser,
# proxyman). Read the plugin.yml rather than trusting a list here.
# The host-only run.sh launchers are excluded from the image via .dockerignore.
# Which containers actually USE a plugin is a separate, per-container decision:
# up.sh wires mcp + egress only for the names in that manifest's `plugins:`
# list. Adding a tool = adding one file; this loop never changes. Runs as
# $USERNAME with the toolchain live — uv via ~/.local/bin, node/npm via the fnm
# env eval — so installers land in the user's home like everything else. The
# "install: required iff a local server" rule is enforced by src/manifest.py at
# derive time (a local plugin missing install: fails up.sh — unless its server
# execs a base tool like mcp-remote, see BASE_IMAGE_BINS and the layer below),
# so here `yq -e` non-zero simply means "no install: block → remote,
# config-only, or base-tool; skip"; set -e still aborts on a failed install.
# yq is pinned and installed HERE, next to its only build-time consumer, so a
# version bump doesn't invalidate the toolchain layers above.
ARG YQ_VERSION=v4.44.3
RUN sudo curl -fsSL "https://github.com/mikefarah/yq/releases/download/${YQ_VERSION}/yq_linux_$(dpkg --print-architecture)" \
        -o /usr/local/bin/yq \
    && sudo chmod +x /usr/local/bin/yq
# ── mcp-remote: a base tool, like bash or python3 ────────────────────────────
# The stdio↔HTTP bridge that lets a remote MCP service reach EVERY agent rather
# than only Claude (a remote `url:` spec can't be wired into cursor/pi). Three
# plugins run it — axiom, browser, proxyman — and each used to carry an
# identical `npm install -g 'mcp-remote@^0.1.38'` in its own install: block.
# That read as harmless duplication and was a trap: all three write into the
# SAME global npm prefix, so whichever install ran last decided the version for
# all of them, and bumping one pin would silently retarget the other two while
# their comments went on claiming a version they no longer got.
#
# So it is installed here instead, once, and src/manifest.py treats it as a
# base-image binary (BASE_IMAGE_BINS) — a plugin whose server execs it needs no
# install: block, the same way nothing installs bash. One ARG is the only place
# the version is stated.
#
# Why the floor matters (the behaviour the pin protects): mcp-remote
# substitutes ${VAR} in a --header value from its OWN process.env at connect
# time (verified in 0.1.38 — the header regex tolerates the space in
# "Bearer ${AXIOM_TOKEN}"). That is what keeps the secret out of every MCP
# config file AND off the command line: argv holds the literal "${AXIOM_TOKEN}",
# never the value. Regress below that floor and the literal ships to the server
# and every agent 401s — with, per mcp-remote's own behaviour, only a warning.
#
# Unconditional, unlike the PLUGINS_ENABLED-gated loop below, because "base
# tool" has to mean always: BASE_IMAGE_BINS excuses a plugin from installing it,
# so a gated install would leave that excuse resting on a condition. Costs ~8M
# in every image. Exec'd directly rather than via `npx` so server startup never
# reaches for the registry — it runs offline behind the egress firewall. Placed
# next to its consumers, like yq above, so a bump doesn't invalidate the
# toolchain layers.
ARG MCP_REMOTE_VERSION=^0.1.38
RUN eval "$(fnm env)" \
    && npm install -g "mcp-remote@${MCP_REMOTE_VERSION}" \
    && npm cache clean --force

COPY --chown=$USERNAME:$USERNAME plugins /opt/plugins
RUN set -e; \
    eval "$(fnm env)"; \
    for f in /opt/plugins/*/plugin.yml; do \
        [ -e "$f" ] || continue; \
        name="$(basename "$(dirname "$f")")"; \
        case " $PLUGINS_ENABLED " in \
            *" $name "*) ;; \
            *) echo "── plugin install (disabled): $name"; continue ;; \
        esac; \
        if ! yq -e -r '.install' "$f" > /tmp/plugin-install.sh 2>/dev/null; then \
            echo "── plugin (config-only, nothing to bake): $name"; \
            continue; \
        fi; \
        echo "── plugin install: $name"; \
        # Keep pipefail: many installers are `curl ... | bash` and must fail
        # this build if the download side fails.
        bash -e -o pipefail /tmp/plugin-install.sh; \
    done; \
    uv cache clean; \
    npm cache clean --force; \
    pip3 cache purge; \
    rm -f /tmp/plugin-install.sh

# ── Agents (descriptor-driven install + runtime index) ───────────────────────
# The per-agent install blocks now live in agents/*/agent.yml. This single loop
# layer is a deliberate cache trade-off: ANY edit under agents/ (a comment fix
# included) or a changed AGENTS_ENABLED invalidates the COPY below and re-runs
# every enabled agent's install (~minutes, network-bound). Accepted per the PLN
# — the loop sits late so the expensive toolchain layers above stay cached; a
# BuildKit npm cache mount is still the documented mitigation if the rebuild
# cost proves annoying. (The npm/pip purges at the end of this RUN address a
# different problem — image SIZE, not rebuild speed. Neither makes the other
# unnecessary.)
COPY --chown=$USERNAME:$USERNAME agents /opt/agents
RUN set -e; \
    eval "$(fnm env)"; \
    for f in /opt/agents/*/agent.yml; do \
        [ -e "$f" ] || continue; \
        name="$(basename "$(dirname "$f")")"; \
        case " $AGENTS_ENABLED " in \
            *" $name "*) ;; \
            *) echo "── agent install (disabled): $name"; continue ;; \
        esac; \
        echo "── agent install: $name"; \
        yq -e -r '.install' "$f" > /tmp/agent-install.sh; \
        # Keep pipefail: many installers are `curl ... | bash` and must fail
        # this build if the download side fails.
        bash -e -o pipefail /tmp/agent-install.sh; \
    done; \
    npm cache clean --force; \
    pip3 cache purge; \
    rm -f /tmp/agent-install.sh

# ── Agent-identity shims ──────────────────────────────────────────────────────
# Each agent CLI is fronted by a shim that loads per-agent MCP credentials from
# ~/.agent-keys/<agent>.env, OVERRIDING inherited env, then execs the real
# binary. This gives per-agent identity (attribution in tools like Obsidian
# Annotated) and makes delegation safe: an agent spawning another never passes
# its own credentials along.
# As of Plugins v2 Phase 3, <agent>.env is COMPLETE (env-scoped + agent-scoped
# secrets composed by up.sh) and common.env is no longer written. The shim
# still sources common.env when present — a one-release transitional guard so an
# older keys dir keeps working; a later release drops that line. The `set -a`
# order (common first, then <agent>) means a fresh per-agent file wins.
# This same loop also renders /usr/local/lib/djinn/agents-index.tsv: stdlib
# runtime consumers (compose_rules.py) cannot parse YAML, so build flattens the
# enabled agent descriptors to TSV once.
# Deliberate UX change vs the old unconditional shim list: a DISABLED agent now
# has no shim at all, so invoking it gets bash's "command not found" instead of
# the shim's "X is not installed in this container" (that branch still guards
# the enabled-but-install-failed case). The manifest's tools: list is the
# pointer for what exists here.
RUN set -e; \
    mkdir -p /home/$USERNAME/.agent-shims; \
    sudo mkdir -p /usr/local/lib/djinn; \
    : > /tmp/agents-index.tsv; \
    for f in /opt/agents/*/agent.yml; do \
        [ -e "$f" ] || continue; \
        name="$(basename "$(dirname "$f")")"; \
        case " $AGENTS_ENABLED " in \
            *" $name "*) ;; \
            *) continue ;; \
        esac; \
        binary="$(yq -r '.binary' "$f")"; \
        rules_file="$(yq -r '.rules_file // ""' "$f")"; \
        if yq -e '.mcp' "$f" >/dev/null 2>&1; then \
            mcp_flag=true; \
            printf '#!/bin/bash\nAGENT=%s\nKEYS="$HOME/.agent-keys"\nset -a\n[ -f "$KEYS/common.env" ] && . "$KEYS/common.env"\n[ -f "$KEYS/$AGENT.env" ] && . "$KEYS/$AGENT.env"\nset +a\nREAL=$(type -aP %s | grep -v ".agent-shims" | head -1)\n[ -n "$REAL" ] || { echo "%s is not installed in this container" >&2; exit 127; }\nexec "$REAL" "$@"\n' "$binary" "$binary" "$binary" > "/home/$USERNAME/.agent-shims/$binary"; \
            chmod +x "/home/$USERNAME/.agent-shims/$binary"; \
        else \
            mcp_flag=false; \
        fi; \
        printf '%s\t%s\t%s\tmcp:%s\n' "$name" "$binary" "$rules_file" "$mcp_flag" >> /tmp/agents-index.tsv; \
    done; \
    LC_ALL=C sort -t "$(printf '\t')" -k1,1 /tmp/agents-index.tsv \
        | sudo tee /usr/local/lib/djinn/agents-index.tsv >/dev/null; \
    sudo chmod 644 /usr/local/lib/djinn/agents-index.tsv; \
    rm -f /tmp/agents-index.tsv

# Shims must win over the real binaries in EVERY shell — interactive,
# non-interactive (`docker exec ... claude`, `ssh host 'claude -p'`, VS Code
# tasks), and login. ENV covers all of them; .bashrc alone would not (its
# export sits after Ubuntu's non-interactive guard). The fnm default-alias
# bin is a STABLE path to node + the npm-global CLIs (the per-shell
# fnm_multishells path only exists after `fnm env`), so the shims' `type -aP`
# resolves the real binaries without an interactive shell.
ENV PATH="/home/$USERNAME/.agent-shims:/home/$USERNAME/.local/bin:/home/$USERNAME/.fnm/aliases/default/bin:/home/$USERNAME/.fnm:$PATH"

# ENV covers `docker exec` (attach mode) but sshd builds its session env via
# PAM and ignores it — so SSH sessions (incl. non-interactive `ssh host
# 'claude -p'`) need the PATH in /etc/environment, which pam_env applies to
# every SSH session type. $PATH here is the resolved ENV value set above.
RUN echo "PATH=$PATH" | sudo tee /etc/environment >/dev/null

# Auth/state mountpoints are pre-created from enabled agents' state_dirs so the
# first mount of those named volumes inherits coder ownership.
RUN set -e; \
    mkdir -p /home/$USERNAME/.config/gh; \
    for f in /opt/agents/*/agent.yml; do \
        [ -e "$f" ] || continue; \
        name="$(basename "$(dirname "$f")")"; \
        case " $AGENTS_ENABLED " in \
            *" $name "*) ;; \
            *) continue ;; \
        esac; \
        yq -r '.state_dirs[]?.path // ""' "$f" | while IFS= read -r rel; do \
            [ -n "$rel" ] || continue; \
            mkdir -p "/home/$USERNAME/$rel"; \
        done; \
    done

# ── Workspace ─────────────────────────────────────────────────────────────────
RUN sudo mkdir -p /workspace && sudo chown $USERNAME:$USERNAME /workspace

WORKDIR /workspace

# ── Entrypoint ────────────────────────────────────────────────────────────────
COPY --chown=$USERNAME:$USERNAME src/entrypoint.sh /home/$USERNAME/entrypoint.sh
RUN chmod +x /home/$USERNAME/entrypoint.sh

# Back to root for the entrypoint (drops to coder context / runs sshd)
USER root

# ── Egress firewall (init-firewall.sh, run by entrypoint) ────────────────────
# Needs NET_ADMIN + NET_RAW at runtime. Kept late in the file for layer cache.
RUN apt-get update && apt-get install -y \
    iptables ipset iproute2 dnsutils aggregate dnsmasq \
    && rm -rf /var/lib/apt/lists/*

COPY src/init-firewall.sh /usr/local/bin/init-firewall.sh
RUN chmod +x /usr/local/bin/init-firewall.sh
COPY src/egress_broker_firewall.sh /usr/local/bin/egress_broker_firewall.sh
RUN chmod +x /usr/local/bin/egress_broker_firewall.sh

# Per-org git credential router (entrypoint installs it as the github.com
# credential helper). Routes by repo owner to GH_TOKEN_<owner>, falling back to
# the container GH_TOKEN then gh's human login. See src/git-credential-org.sh.
COPY src/git-credential-org.sh /usr/local/bin/git-credential-org
RUN chmod +x /usr/local/bin/git-credential-org

# ── SSH server (always installed, runs only when SSH_ENABLED=true) ──────────
# One image everywhere: Mac attach-mode and any remote Linux host. The manifest's ssh:
# section turns sshd on at RUNTIME (entrypoint injects SSH_AUTHORIZED_KEY).
RUN apt-get update && apt-get install -y openssh-server \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /var/run/sshd \
    && sed -i \
        -e 's/#PasswordAuthentication yes/PasswordAuthentication no/' \
        -e 's/#PubkeyAuthentication yes/PubkeyAuthentication yes/' \
        -e 's/#PermitRootLogin prohibit-password/PermitRootLogin no/' \
        /etc/ssh/sshd_config \
    && echo "AllowUsers $USERNAME" >> /etc/ssh/sshd_config

# ── Agent-config wiring module (up.sh execs it after boot) ──────────────────
# Stdlib-only python3; up.sh pipes it a JSON payload over docker exec -i.
# Last COPY on purpose: this is the most edit-prone file in the image, and
# here a change re-runs only this layer, not the apt installs above. chmod:
# COPY keeps the build-context mode, and a umask-077 clone would otherwise
# bake a root-only 600 file the coder-user exec can't read.
COPY src/wire_plugins.py /usr/local/lib/djinn/wire_plugins.py
RUN chmod 644 /usr/local/lib/djinn/wire_plugins.py

# Composes each agent's global rules file = base rules (read-only /agent-rules
# mount) + the AGENTS.md fragments of the plugins a container enables. up.sh
# runs it at `up`; the rules-compose.bashrc hook (below) re-runs it per shell.
COPY src/compose_rules.py /usr/local/lib/djinn/compose_rules.py
RUN chmod 644 /usr/local/lib/djinn/compose_rules.py

# Merges the manifest's repo list into /workspace/dev.code-workspace on every
# `up` (idempotent — preserves agent-managed worktree entries; layout v2).
COPY src/code_workspace.py /usr/local/lib/djinn/code_workspace.py
RUN chmod 644 /usr/local/lib/djinn/code_workspace.py

COPY src/egress_log.py /usr/local/lib/djinn/egress_log.py
COPY src/egress_broker_host.py /usr/local/lib/djinn/egress_broker_host.py
COPY src/egress_broker.py /usr/local/lib/djinn/egress_broker.py
COPY src/egress_nflog.py /usr/local/lib/djinn/egress_nflog.py
COPY src/egress_request.py /usr/local/lib/djinn/egress_request.py
RUN chmod 644 /usr/local/lib/djinn/egress_log.py \
    /usr/local/lib/djinn/egress_broker_host.py \
    /usr/local/lib/djinn/egress_broker.py \
    /usr/local/lib/djinn/egress_nflog.py \
    /usr/local/lib/djinn/egress_request.py

COPY src/egress_request.py /usr/local/bin/request-egress
RUN chmod 755 /usr/local/bin/request-egress

# Dedicated uid for the in-container egress broker. iptables (B3) exempts this
# owner from REDIRECT so the broker's own upstream dials are not looped back.
RUN useradd --system --no-create-home --shell /usr/sbin/nologin djinnbroker

ENV SSH_ENABLED=false

# ── Remote session tools: tmux + mosh (RFC 04) ───────────────────────────────
# tmux gives every SSH-reachable container one durable, shared session (both
# phone and laptop attach to the same view; agents survive disconnects). mosh
# rides UDP for flaky mobile networks — reached only over the operator's
# WireGuard/VPN tunnel, never a public listener. mosh requires a UTF-8
# locale — update-locale writes /etc/default/locale, which PAM reads for
# SSH sessions (the ENV below only covers entrypoint/docker-exec processes;
# sshd builds its env from PAM and would otherwise run C/POSIX and make
# mosh-server abort with 'needs a UTF-8 native locale').
RUN apt-get update && apt-get install -y \
    tmux mosh locales \
    && rm -rf /var/lib/apt/lists/* \
    && locale-gen en_US.UTF-8 \
    && update-locale LANG=en_US.UTF-8
ENV LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8

COPY --chown=$USERNAME:$USERNAME src/tmux.conf /home/$USERNAME/.tmux.conf

# Pin mosh-server to the firewalled/published UDP range: /usr/local/bin wins
# over /usr/bin, so the client-launched `mosh-server new` resolves to the
# wrapper regardless of client configuration.
COPY src/mosh-server-wrapper.sh /usr/local/bin/mosh-server
RUN chmod +x /usr/local/bin/mosh-server

# Agent-blind idle notifier: tmux.conf's silence hook runs it when NTFY_URL
# is present in the environment (remote.notify: ntfy).
COPY src/tmux-notify.sh /usr/local/bin/tmux-notify.sh
RUN chmod +x /usr/local/bin/tmux-notify.sh

# Recompose agent global rules (base + enabled-plugin fragments) on each
# interactive shell. Sourced BEFORE the tmux-landing hook, which execs tmux and
# never returns — placing it after would skip it in the login shell.
COPY src/rules-compose.bashrc /usr/local/share/rules-compose.bashrc
RUN echo '' >> /home/$USERNAME/.bashrc \
    && echo '# Recompose agent rules (base + enabled-plugin fragments) on interactive shells' >> /home/$USERNAME/.bashrc \
    && echo '. /usr/local/share/rules-compose.bashrc' >> /home/$USERNAME/.bashrc

# ── Container freshness readout (PLN - Container Freshness Readout) ───────────
# A passive, no-network, one-line readout of how old this container's config is
# (last `up` + image build date), printed to interactive shells so the human
# decides when to re-`up`. Stamps are written into /etc/environment by up.sh
# after boot; freshness.py (stdlib-only, unit-tested) formats the relative age.
# Sourced BEFORE tmux-landing so that hook stays the last line of .bashrc, and
# so it prints once — in the tmux pane, not the outer shell tmux replaces.
COPY src/freshness.py /usr/local/lib/djinn/freshness.py
RUN chmod 644 /usr/local/lib/djinn/freshness.py
COPY --chown=$USERNAME:$USERNAME src/freshness-landing.bashrc /usr/local/share/freshness-landing.bashrc
RUN echo '' >> /home/$USERNAME/.bashrc \
    && echo '# PLN Container Freshness: one-line dim config-age readout (interactive)' >> /home/$USERNAME/.bashrc \
    && echo '. /usr/local/share/freshness-landing.bashrc' >> /home/$USERNAME/.bashrc

# Land interactive SSH/mosh logins in the shared tmux session. The logic
# lives in a sourced file (lintable, readable); the hook must be the LAST
# line of .bashrc so fnm/shim PATH setup has already run when tmux execs.
COPY src/tmux-landing.bashrc /usr/local/share/tmux-landing.bashrc
RUN echo '' >> /home/$USERNAME/.bashrc \
    && echo '# RFC 04: SSH/mosh logins land in a shared tmux session (keep last)' >> /home/$USERNAME/.bashrc \
    && echo '. /usr/local/share/tmux-landing.bashrc' >> /home/$USERNAME/.bashrc

# VS Code / Cursor "Attach to Running Container" reads this: attach as
# coder (not root) and open /workspace by default.
LABEL devcontainer.metadata='{"remoteUser":"coder","workspaceFolder":"/workspace"}'

EXPOSE 22

ENTRYPOINT ["/home/coder/entrypoint.sh"]
