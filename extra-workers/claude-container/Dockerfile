# syntax=docker/dockerfile:1
# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors
#
# The Claude Code worker (README.md). Build from the repository root, which the backend is
# installed from, editable: backend/hatch_build.py refuses a wheel built without the interface,
# which a worker never serves.
#
#     docker build -f extra-workers/claude-container/Dockerfile -t robinauts-claude-worker .
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
COPY extra-workers/claude-container/pyproject.toml extra-workers/claude-container/uv.lock \
    extra-workers/claude-container/
COPY extra-workers/claude-container/src extra-workers/claude-container/src
RUN cd extra-workers/claude-container && uv sync --locked --no-dev

# Nothing runs as root. The transcripts and the workspaces are the two volumes it writes.
RUN useradd --system --create-home --home-dir /var/lib/robinauts --shell /usr/sbin/nologin \
        robinauts \
    && mkdir -p /var/lib/claude /workspace \
    && chown robinauts:robinauts /var/lib/claude /workspace
USER robinauts
ENV CLAUDE_CONFIG_DIR=/var/lib/claude \
    ROBINAUTS_CLAUDE_WORKSPACES=/workspace \
    ROBINAUTS_CLAUDE_ACTIVITY_FILE=/tmp/robinauts-claude-worker.alive \
    PATH=/opt/robinauts/venv/bin:$PATH
VOLUME ["/var/lib/claude", "/workspace"]

# Healthy while the worker has touched its activity file within five minutes.
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-m", "robinauts_claude_worker", "health"]
ENTRYPOINT ["python", "-m", "robinauts_claude_worker"]
