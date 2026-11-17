# syntax=docker/dockerfile:1
# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors
#
# Robinauts' image: the backend with its built interface, oneberth's web application and neorc's
# manager, the image the desktop's Docker shape runs the backend from (docs/deployment.md, "The
# desktop with Docker"). It serves the desktop alone for now. `robinauts desktop` starts it as
# `robinauts desktop-backend`, the data directory mounted at /data. Build from the repository
# root:
#
#     docker build -t robinauts .
#
# The interface stage packs the node packages of neorc and oneberth itself, with
# scripts/ui-packages-from-source.sh. The build needs nothing but the repository, so a commit
# alone builds it: docker build https://github.com/the-guish/robinauts.git#<commit>
#
# The image carries the same build of robinauts as the `robinauts desktop` that starts it: the
# workers on the host parse the same configuration and speak a wire with no version handshake.

# The interface, built from its lock as scripts/check-frontend.sh builds it.
FROM node:24-slim AS interface
# git, which scripts/ui-packages-from-source.sh fetches neorc and oneberth with.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/robinauts/src
# The licence policy the build checks the bundle against, and the API it types the client with.
COPY DEPENDENCIES.md ./
COPY backend/openapi.json backend/
# The pin of neorc, which names the commit the node packages come from.
COPY backend/pyproject.toml backend/
COPY scripts/ui-packages-from-source.sh scripts/
COPY frontend frontend
RUN scripts/ui-packages-from-source.sh \
    && cd frontend && npm ci --ignore-scripts && CHECK_BUNDLED=1 npm run build

FROM python:3.12-slim

# git, which uv builds neorc and oneberth from (neorc-from-source), and the certificates it
# checks GitHub's against.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git \
    && rm -rf /var/lib/apt/lists/*

# uv, at the version scripts/tool-versions.sh pins for CI.
COPY --from=ghcr.io/astral-sh/uv:0.12.6 /uv /uvx /bin/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PYTHON_PREFERENCE=only-system \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/robinauts/venv

WORKDIR /opt/robinauts/src
# The backend, installed editable from the checkout's layout: it finds the interface in
# frontend/dist and the handlers Robinauts ships in orchestration/, beside backend/.
COPY LICENSE NOTICE ./
COPY backend/pyproject.toml backend/uv.lock backend/hatch_build.py backend/
COPY backend/src backend/src
COPY orchestration orchestration
COPY --from=interface /opt/robinauts/src/frontend/dist frontend/dist
RUN cd backend && uv sync --locked --no-dev

# Nothing runs as root. The launcher runs the container as the person's own user, whom the image
# does not know: everything here is readable by any user, and the backend writes in /data alone.
RUN useradd --system --create-home --home-dir /var/lib/robinauts --shell /usr/sbin/nologin \
    robinauts
USER robinauts
ENV PATH=/opt/robinauts/venv/bin:$PATH

# The backend as the launcher starts it: it reads the launch secret on its standard input, says
# `ready` on its standard output, and stops at SIGTERM.
CMD ["robinauts", "desktop-backend"]
