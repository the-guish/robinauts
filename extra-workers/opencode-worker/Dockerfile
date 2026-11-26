# syntax=docker/dockerfile:1
# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors
#
# The OpenCode worker (README.md): the backend's own worker module with the opencode
# engine plugged in, in an image the server starts with `docker run -i`. Build from the
# repository root, which the backend is installed from, editable: backend/hatch_build.py
# refuses a wheel built without the interface, which a worker never serves.
#
#     docker build -f extra-workers/opencode-worker/Dockerfile -t robinauts-opencode-worker .
#
# The image must carry the same build of robinauts as the server it serves: the two parse the
# same configuration file and speak a wire with no version handshake yet.

# OpenCode, at a pinned release: the single binary its project publishes for Linux, fetched in a
# stage of its own so that the image carries neither curl nor the archive. The 1.x line serves the
# HTTP API this worker speaks; 2.x serves another (README.md, "Versions").
FROM debian:bookworm-slim AS fetch
ARG OPENCODE_VERSION=1.19.6
ARG TARGETARCH
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
RUN set -eu; \
    case "${TARGETARCH:-amd64}" in amd64) arch=x64;; arm64) arch=arm64;; \
        *) echo "no OpenCode build for ${TARGETARCH}" >&2; exit 1;; esac; \
    mkdir /out; \
    curl -fsSL "https://github.com/anomalyco/opencode/releases/download/v${OPENCODE_VERSION}/opencode-linux-${arch}.tar.gz" \
        | tar -xz -C /out opencode; \
    chmod 0755 /out/opencode

FROM python:3.12-slim

# What OpenCode's tools reach for in a workspace: git for the agent, ripgrep for its search.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git ripgrep \
    && rm -rf /var/lib/apt/lists/*

# uv, at the version scripts/tool-versions.sh pins for CI.
COPY --from=ghcr.io/astral-sh/uv:0.12.6 /uv /uvx /bin/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PYTHON_PREFERENCE=only-system \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/robinauts/venv

WORKDIR /opt/robinauts/src
# The backend the worker is composed from, and the licence files its build stages.
COPY LICENSE NOTICE ./
COPY backend/pyproject.toml backend/uv.lock backend/hatch_build.py backend/
COPY backend/src backend/src
COPY extra-workers/opencode-worker/pyproject.toml extra-workers/opencode-worker/uv.lock \
    extra-workers/opencode-worker/
COPY extra-workers/opencode-worker/src extra-workers/opencode-worker/src
# What the extra workers share, installed editable from where the worker's lock says it is.
COPY extra-workers/worker-common/pyproject.toml extra-workers/worker-common/
COPY extra-workers/worker-common/src extra-workers/worker-common/src
# neorc-from-source: the worker imports neither neorc nor oneberth, which uv would build from git.
RUN cd extra-workers/opencode-worker && uv sync --locked --no-dev \
    --no-install-package neorc --no-install-package neorc-core --no-install-package oneberth

# The binary, from the stage that fetched it.
COPY --from=fetch /out/opencode /usr/local/bin/opencode

# Nothing runs as root. OpenCode's state and the workspaces are the two volumes it writes.
RUN useradd --system --create-home --home-dir /var/lib/robinauts --shell /usr/sbin/nologin \
        robinauts \
    && mkdir -p /var/lib/opencode /workspace \
    && chown robinauts:robinauts /var/lib/opencode /workspace
USER robinauts
# OpenCode keeps its sessions in a SQLite file under XDG_DATA_HOME, and each conversation gets a
# directory under the workspaces. The engine starts it with everything that would phone home
# switched off (src/robinauts_opencode_worker/server.py).
ENV XDG_DATA_HOME=/var/lib/opencode/data \
    XDG_CONFIG_HOME=/var/lib/opencode/config \
    XDG_STATE_HOME=/var/lib/opencode/state \
    XDG_CACHE_HOME=/var/lib/opencode/cache \
    ROBINAUTS_OPENCODE_WORKSPACES=/workspace \
    PATH=/opt/robinauts/venv/bin:$PATH
VOLUME ["/var/lib/opencode", "/workspace"]

# The worker, as the server starts it in a process of its own: it checks its environment and
# the configuration, then reads its secret on its standard input, says `ready` on its standard
# output, and stops at SIGTERM.
CMD ["python", "-m", "robinauts_opencode_worker"]
