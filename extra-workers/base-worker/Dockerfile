# syntax=docker/dockerfile:1
# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors
#
# The base worker (README.md): the backend's own worker module, with the engines the backend
# ships and nothing else, in an image the server starts with `docker run -i`. Build from the
# repository root, which the backend is installed from, editable: backend/hatch_build.py
# refuses a wheel built without the interface, which a worker never serves.
#
#     docker build -f extra-workers/base-worker/Dockerfile -t robinauts-base-worker .
#
# The image must carry the same build of robinauts as the server it serves: the two parse the
# same configuration file and speak a wire with no version handshake yet.

FROM python:3.12-slim

# git, for the clones of robinauts_worker_common.clone (../worker-common/README.md), and the
# certificates it checks the GitHub server's against.
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
# The backend the worker runs, and the licence files its build stages.
COPY LICENSE NOTICE ./
COPY backend/pyproject.toml backend/uv.lock backend/hatch_build.py backend/
COPY backend/src backend/src
COPY extra-workers/base-worker/pyproject.toml extra-workers/base-worker/uv.lock \
    extra-workers/base-worker/
# What the extra workers share, installed editable from where the worker's lock says it is.
COPY extra-workers/worker-common/pyproject.toml extra-workers/worker-common/
COPY extra-workers/worker-common/src extra-workers/worker-common/src
# neorc-from-source: the worker never imports neorc, and this image has no Node.js to build it.
RUN cd extra-workers/base-worker && uv sync --locked --no-dev \
    --no-install-package neorc --no-install-package neorc-core --no-install-package neorc-ui

# Nothing runs as root, and nothing is written: the engines keep their state in memory.
RUN useradd --system --create-home --home-dir /var/lib/robinauts --shell /usr/sbin/nologin \
    robinauts
USER robinauts
ENV PATH=/opt/robinauts/venv/bin:$PATH

# The backend's worker module, as the server starts it in a process of its own: it reads its
# secret on its standard input, says `ready` on its standard output, and stops at SIGTERM. In
# front of it, the extra workers' shared start, which checks the GitHub token it may be handed
# (`--env-file`, ../worker-common/README.md) before the secret is read.
CMD ["python", "-m", "robinauts_worker_common"]
