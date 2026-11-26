# syntax=docker/dockerfile:1
# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors
#
# The Codex worker (README.md): the backend's own worker module with the codex
# engine plugged in, in an image the server starts with `docker run -i`. Build from the
# repository root, which the backend is installed from, editable: backend/hatch_build.py
# refuses a wheel built without the interface, which a worker never serves.
#
#     docker build -f extra-workers/codex-worker/Dockerfile -t robinauts-codex-worker .
#
# The image must carry the same build of robinauts as the server it serves: the two parse the
# same configuration file and speak a wire with no version handshake yet.

# Codex, at a pinned release: the static binary OpenAI publishes for Linux, fetched in a stage
# of its own so that the image carries neither curl nor the archive. Bump CODEX_VERSION with the
# protocol it speaks (src/robinauts_codex_worker/rpc.py, engine.py).
FROM debian:bookworm-slim AS fetch
ARG CODEX_VERSION=0.172.0
ARG TARGETARCH
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
RUN set -eu; \
    case "${TARGETARCH:-amd64}" in amd64) triple=x86_64;; arm64) triple=aarch64;; \
        *) echo "no Codex build for ${TARGETARCH}" >&2; exit 1;; esac; \
    mkdir /out; \
    curl -fsSL "https://github.com/openai/codex/releases/download/rust-v${CODEX_VERSION}/codex-${triple}-unknown-linux-musl.tar.gz" \
        | tar -xz -C /out; \
    mv "/out/codex-${triple}-unknown-linux-musl" /out/codex; \
    chmod 0755 /out/codex

FROM python:3.12-slim

# What Codex's tools reach for in a workspace: git for the agent, ripgrep for its search.
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
COPY extra-workers/codex-worker/pyproject.toml extra-workers/codex-worker/uv.lock \
    extra-workers/codex-worker/
COPY extra-workers/codex-worker/src extra-workers/codex-worker/src
# What the extra workers share, installed editable from where the worker's lock says it is.
COPY extra-workers/worker-common/pyproject.toml extra-workers/worker-common/
COPY extra-workers/worker-common/src extra-workers/worker-common/src
# neorc-from-source: the worker imports neither neorc nor oneberth, which uv would build from git.
RUN cd extra-workers/codex-worker && uv sync --locked --no-dev \
    --no-install-package neorc --no-install-package neorc-core --no-install-package oneberth

# The binary, from the stage that fetched it.
COPY --from=fetch /out/codex /usr/local/bin/codex

# Nothing runs as root. Codex's home and the workspaces are the two volumes it writes.
RUN useradd --system --create-home --home-dir /var/lib/robinauts --shell /usr/sbin/nologin \
        robinauts \
    && mkdir -p /var/lib/codex /workspace \
    && chown robinauts:robinauts /var/lib/codex /workspace
USER robinauts
# Codex keeps its threads, its state and, in personal mode, the login under CODEX_HOME, and each
# conversation gets a directory under the workspaces. Analytics, feedback, OpenTelemetry and the
# update check are switched off on its command line (src/robinauts_codex_worker/config.py).
ENV CODEX_HOME=/var/lib/codex \
    ROBINAUTS_CODEX_WORKSPACES=/workspace \
    PATH=/opt/robinauts/venv/bin:$PATH
VOLUME ["/var/lib/codex", "/workspace"]

# The worker, as the server starts it in a process of its own: it checks its environment and
# the configuration, then reads its secret on its standard input, says `ready` on its standard
# output, and stops at SIGTERM.
CMD ["python", "-m", "robinauts_codex_worker"]
