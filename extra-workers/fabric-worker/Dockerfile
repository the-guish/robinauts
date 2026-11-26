# syntax=docker/dockerfile:1
# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors
#
# The Fabric worker (README.md): the backend's own worker module with the fabric engine plugged
# in, which runs each turn on Claude Code through NVIDIA NeMo Fabric's Claude adapter, in an
# image the server starts with `docker run -i`. Build from the repository root, which the
# backend is installed from, editable: backend/hatch_build.py refuses a wheel built without the
# interface, which a worker never serves.
#
#     docker build -f extra-workers/fabric-worker/Dockerfile -t robinauts-fabric-worker .
#
# The image must carry the same build of robinauts as the server it serves: the two parse the
# same configuration file and speak a wire with no version handshake yet.

FROM python:3.12-slim

# What Claude Code's tools reach for in a workspace: git for the agent, ripgrep for Grep.
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
COPY extra-workers/fabric-worker/pyproject.toml extra-workers/fabric-worker/uv.lock \
    extra-workers/fabric-worker/
COPY extra-workers/fabric-worker/src extra-workers/fabric-worker/src
# What the extra workers share, installed editable from where the worker's lock says it is.
COPY extra-workers/worker-common/pyproject.toml extra-workers/worker-common/
COPY extra-workers/worker-common/src extra-workers/worker-common/src
# neorc-from-source: the worker imports neither neorc nor oneberth, which uv would build from git.
RUN cd extra-workers/fabric-worker && uv sync --locked --no-dev \
    --no-install-package neorc --no-install-package neorc-core --no-install-package oneberth

# Nothing runs as root. Claude Code's configuration, the engine's state and the workspaces are
# the three volumes it writes.
RUN useradd --system --create-home --home-dir /var/lib/robinauts --shell /usr/sbin/nologin \
        robinauts \
    && mkdir -p /var/lib/claude /var/lib/robinauts-fabric /workspace \
    && chown robinauts:robinauts /var/lib/claude /var/lib/robinauts-fabric /workspace
USER robinauts
# Fabric runs its adapter host with ADAPTER_PYTHON, the environment that has the Claude adapter.
# The engine hands Claude Code CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC itself, since the
# adapter passes on only the variables it names; NeMo Relay, Fabric's telemetry, is not
# installed (docs/specs/core.md, "Nothing phones home").
ENV CLAUDE_CONFIG_DIR=/var/lib/claude \
    ROBINAUTS_FABRIC_WORKSPACES=/workspace \
    ROBINAUTS_FABRIC_STATE=/var/lib/robinauts-fabric \
    ADAPTER_PYTHON=/opt/robinauts/venv/bin/python \
    PATH=/opt/robinauts/venv/bin:$PATH
VOLUME ["/var/lib/claude", "/var/lib/robinauts-fabric", "/workspace"]

# The worker, as the server starts it in a process of its own: it checks its environment and
# the configuration, then reads its secret on its standard input, says `ready` on its standard
# output, and stops at SIGTERM.
CMD ["python", "-m", "robinauts_fabric_worker"]
