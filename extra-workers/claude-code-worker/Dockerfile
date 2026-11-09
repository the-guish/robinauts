# syntax=docker/dockerfile:1
# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors
#
# The Claude Code worker (README.md): the backend's own worker module with the claude-code
# engine plugged in, in an image the server starts with `docker run -i`. Build from the
# repository root, which the backend is installed from, editable: backend/hatch_build.py
# refuses a wheel built without the interface, which a worker never serves.
#
#     docker build -f extra-workers/claude-code-worker/Dockerfile -t robinauts-claude-code-worker .
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
COPY extra-workers/claude-code-worker/pyproject.toml extra-workers/claude-code-worker/uv.lock \
    extra-workers/claude-code-worker/
COPY extra-workers/claude-code-worker/src extra-workers/claude-code-worker/src
# neorc-from-source: the worker never imports neorc, and this image has no Node.js to build it.
RUN cd extra-workers/claude-code-worker && uv sync --locked --no-dev \
    --no-install-package neorc --no-install-package neorc-core --no-install-package neorc-ui

# Nothing runs as root. The transcripts and the workspaces are the two volumes it writes.
RUN useradd --system --create-home --home-dir /var/lib/robinauts --shell /usr/sbin/nologin \
        robinauts \
    && mkdir -p /var/lib/claude /workspace \
    && chown robinauts:robinauts /var/lib/claude /workspace
USER robinauts
# Claude Code keeps the transcripts under CLAUDE_CONFIG_DIR, and each conversation gets a
# directory under the workspaces. It sends nothing but the turns: no telemetry, no error
# reports, no update checks (docs/specs/core.md, "Nothing phones home").
ENV CLAUDE_CONFIG_DIR=/var/lib/claude \
    ROBINAUTS_CLAUDE_WORKSPACES=/workspace \
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 \
    PATH=/opt/robinauts/venv/bin:$PATH
VOLUME ["/var/lib/claude", "/workspace"]

# The worker, as the server starts it in a process of its own: it checks its environment and
# the configuration, then reads its secret on its standard input, says `ready` on its standard
# output, and stops at SIGTERM.
CMD ["python", "-m", "robinauts_claude_worker"]
