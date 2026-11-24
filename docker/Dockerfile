# syntax=docker/dockerfile:1
# fleetmux for a headless Linux server. Build it with docker/build.sh: the build context is `git archive` of one
# commit, never the working tree. The release inside is installed by scripts/install_release.sh (the same installer
# as every other machine) and checked against deps.yaml during the build, so an image that misses deps.yaml is never
# tagged. The agent CLIs (Claude Code, Codex, pi, opencode) are not in the image: `docker/fleetmux-docker up`
# downloads the versions pinned in deps.yaml `agent_installs` into a volume at /opt/agents (docker/install_agents.py)
# and checks them there. Logins are not in the image either: the wrapper mounts the host home.

ARG PYTHON_IMAGE=python:3.14-slim-trixie
ARG NODE_IMAGE=node:22-trixie-slim
ARG REDIS_IMAGE=redis:8.6-trixie
ARG RUST_IMAGE=rust:1-slim-trixie

# ---- fleetmux terminal (Rust; rust/rust-toolchain.toml picks the toolchain). fleetmux-web is desktop-only.
FROM ${RUST_IMAGE} AS rust
WORKDIR /src
COPY rust/ rust/
# the zh-Hans string table the terminal embeds at build time (include_str!, docs/design.md §63)
COPY fleetmux/locales/ fleetmux/locales/
RUN --mount=type=cache,target=/usr/local/cargo/registry \
    --mount=type=cache,target=/src/rust/target \
    cd rust && cargo build --release --locked -p fleetmux && install -D target/release/fleetmux /out/fleetmux

# ---- tmux >= 3.6 (deps.yaml; Debian trixie ships 3.5a)
FROM ${PYTHON_IMAGE} AS tmux
ARG TMUX_VERSION=3.6a
ARG TMUX_SHA256=b6d8d9c76585db8ef5fa00d4931902fa4b8cbe8166f528f44fc403961a3f3759
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential pkg-config libevent-dev libncurses-dev curl ca-certificates \
 && curl -fsSLo /tmp/tmux.tgz "https://github.com/tmux/tmux/releases/download/${TMUX_VERSION}/tmux-${TMUX_VERSION}.tar.gz" \
 && echo "${TMUX_SHA256}  /tmp/tmux.tgz" | sha256sum -c - \
 && tar xzf /tmp/tmux.tgz -C /tmp && cd "/tmp/tmux-${TMUX_VERSION}" \
 && ./configure --prefix=/usr/local && make -j"$(nproc)" && make install

FROM ${NODE_IMAGE} AS node

FROM ${REDIS_IMAGE} AS redis

# ---- runtime
FROM ${PYTHON_IMAGE}
RUN apt-get update && apt-get install -y --no-install-recommends \
      git openssh-client ca-certificates curl procps less libstdc++6 libevent-core-2.1-7t64 libncursesw6 \
 && rm -rf /var/lib/apt/lists/*
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx
RUN mkdir /opt/agents
COPY --from=redis /usr/local/bin/redis-server /usr/local/bin/redis-server
COPY --from=tmux /usr/local/bin/tmux /usr/local/bin/tmux
ENV PATH=/opt/fleetmux/current/bin:/opt/agents/bin:$PATH \
    FLEETMUX_RELEASES=/opt/fleetmux/releases \
    FLEETMUX_BACKEND=fleetmux \
    LANG=C.UTF-8 TERM=xterm-256color \
    DISABLE_AUTOUPDATER=1

# The release. The source tarball is rebuilt from the context (= git archive of FLEETMUX_SHA); the release dir is the
# 12-char sha so the private Redis socket path stays short.
ARG FLEETMUX_SHA
# Optional channel packages (deps.yaml optional_python_packages; install_release.sh does not install them).
ARG CHANNEL_PACKAGES="websocket-client==1.8.0 lark-oapi==1.5.3"
COPY --from=rust /out/fleetmux /tmp/fleetmux-bin/fleetmux
RUN --mount=type=bind,target=/tmp/fleetmux-src set -e; \
    case "$FLEETMUX_SHA" in *[!0-9a-f]*|'') echo "FLEETMUX_SHA must be the full commit sha (use docker/build.sh)" >&2; exit 1 ;; esac; \
    short=$(printf %.12s "$FLEETMUX_SHA"); \
    tar czf /tmp/fleetmux-src.tgz -C /tmp/fleetmux-src .; \
    cd /tmp/fleetmux-bin && sha256sum fleetmux > SHA256SUMS; \
    printf '{"source_sha": "%s", "platform": "docker-linux-%s", "label": "docker", "same_sha_build": true}\n' \
      "$FLEETMUX_SHA" "$(dpkg --print-architecture)" > manifest.json; \
    FLEETMUX_DEPS_DEFER="claude codex pi opencode" FLEETMUX_PYTHON=/usr/local/bin/python3.14 \
      /tmp/fleetmux-src/scripts/install_release.sh "$short" /tmp/fleetmux-src.tgz \
      --bin-dir /tmp/fleetmux-bin --headless --tool-sha "$FLEETMUX_SHA"; \
    if [ -n "$CHANNEL_PACKAGES" ]; then "$FLEETMUX_RELEASES/$short/.venv/bin/pip" install -q --no-cache-dir $CHANNEL_PACKAGES; fi; \
    ln -s "$FLEETMUX_RELEASES/$short" /opt/fleetmux/current; \
    ln -s /opt/fleetmux/current/.venv/bin/cmux-fleet /usr/local/bin/cmux-fleet; \
    rm -rf /tmp/fleetmux-src.tgz /tmp/fleetmux-bin /root/.cache

# A long-lived box: fleets are started inside it with `docker exec` (docker/fleetmux-docker) and resume themselves.
CMD ["sleep", "infinity"]
