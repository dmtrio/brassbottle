# djinn-egress — one image for the egress broker and the admin daemon.
#
# Both services are `restart: unless-stopped` containers in the djinn-egress
# compose project (src/egress_service.py renders the file; `djinn egress start`
# drives it), so no terminal has to stay open for a bottle to file an egress
# request. Compose picks the entry per service via `command:` — there is no
# ENTRYPOINT here on purpose.
#
# The first stage builds the Vue admin SPA from admin/ui/; the final stage
# copies only the built dist/ so the image does not carry node_modules or the
# TypeScript sources. Building the UI is part of the image build so a broken
# UI cannot reach the host.

FROM node:22-bookworm-slim AS ui

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build/admin/ui
COPY admin/ui/package.json admin/ui/package-lock.json ./
RUN npm ci

COPY admin/ui/ .
COPY admin/contract/ /build/admin/contract/
RUN npm run build

FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 ca-certificates curl docker.io \
    && rm -rf /var/lib/apt/lists/*

# yq is not packaged on bookworm: install the pinned mikefarah/yq release
# binary for the build architecture, sha256-verified — never `latest` (same
# pinning discipline as the bottle image's installs).
ARG YQ_VERSION=v4.44.3
ARG YQ_SHA256_AMD64=a2c097180dd884a8d50c956ee16a9cec070f30a7947cf4ebf87d5f36213e9ed7
ARG YQ_SHA256_ARM64=0e7e1524f68d91b3ff9b089872d185940ab0fa020a5a9052046ef10547023156
RUN set -eu; arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
      amd64) sha="$YQ_SHA256_AMD64" ;; \
      arm64) sha="$YQ_SHA256_ARM64" ;; \
      *) echo "yq: no pinned sha256 for $arch" >&2; exit 1 ;; \
    esac; \
    curl -fsSL "https://github.com/mikefarah/yq/releases/download/${YQ_VERSION}/yq_linux_${arch}" \
        -o /usr/local/bin/yq \
    && echo "${sha}  /usr/local/bin/yq" | sha256sum -c - \
    && chmod 755 /usr/local/bin/yq

# Same relative layout as a repo checkout: src/common.sh derives the repo root
# from $SCRIPT_DIR/.., so src/, bin/ and the djinn dispatcher land at
# /opt/brassbottle exactly as they sit in the repo.
COPY src/ /opt/brassbottle/src/
COPY bin/ /opt/brassbottle/bin/
COPY djinn /opt/brassbottle/djinn
COPY --from=ui /build/admin/ui/dist /opt/brassbottle/admin/ui/dist
RUN chmod +x /opt/brassbottle/bin/allow-egress.sh /opt/brassbottle/djinn

# The compose file sets `command:` per service:
#   broker: python3 /opt/brassbottle/src/egress_broker_host.py --base-path <DJINN_HOME> \
#             --bind-any --port 8816 --advertise 127.0.0.1:8816
#   admin:  python3 /opt/brassbottle/src/admin_daemon.py --bind-any --port 8817
CMD ["python3", "-c", "print('run via compose command: broker or admin')"]
