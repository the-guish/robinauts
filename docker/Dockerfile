# syntax=docker/dockerfile:1
# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors
#
# One image, two targets, built from the repository's root (docker/compose.yaml
# says so): `api`, the control plane -- the web server, the API and the
# server's own worker -- and `sandbox`, the sandbox worker that runs the turns
# of every sandboxed agent. Both install the same wheel, built here the way
# scripts/build-wheel.sh builds it: the interface first, then the wheel that
# carries it, then the locked requirements beside it, installed with their
# hashes (docs/deployment.md, "Get the wheel").

ARG NODE_IMAGE=node:24-slim
ARG PYTHON_IMAGE=python:3.12-slim

# --- the interface ------------------------------------------------------------
FROM ${NODE_IMAGE} AS interface
WORKDIR /src
COPY frontend/package.json frontend/package-lock.json frontend/.npmrc frontend/
RUN cd frontend && npm ci
# `npm run build` generates the API's types from the backend's snapshot, and
# holds the bundle's licences to the policy in DEPENDENCIES.md.
COPY backend/openapi.json backend/openapi.json
COPY DEPENDENCIES.md ./
COPY frontend/ frontend/
RUN cd frontend && npm run build

# --- the wheel ----------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS wheel
RUN pip install --no-cache-dir uv==0.11.32
WORKDIR /src
COPY LICENSE NOTICE ./
COPY backend/ backend/
COPY --from=interface /src/frontend/dist frontend/dist
RUN cd backend \
    && uv build --wheel --out-dir /out \
    && uv export --locked --no-dev --no-emit-project --no-annotate \
        --format requirements.txt > /out/requirements.txt

# --- what both run on ---------------------------------------------------------
FROM ${PYTHON_IMAGE} AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/opt/robinauts/bin:$PATH
RUN --mount=type=bind,from=wheel,source=/out,target=/tmp/wheel \
    python -m venv /opt/robinauts \
    && /opt/robinauts/bin/pip install --no-cache-dir --require-hashes \
        -r /tmp/wheel/requirements.txt \
    && /opt/robinauts/bin/pip install --no-cache-dir --no-deps /tmp/wheel/*.whl

# --- the control plane --------------------------------------------------------
FROM runtime AS api
RUN useradd --system --create-home --home-dir /home/robinauts robinauts
USER robinauts
EXPOSE 8000
HEALTHCHECK --interval=5s --timeout=3s --start-period=60s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2)"]
CMD ["robinauts", "start", "--host", "0.0.0.0", "--port", "8000"]

# --- the sandbox worker -------------------------------------------------------
# The tools a coding agent expects, and a user of its own; /sandbox is its
# working directory (docs/architecture/sandbox-workers.md).
FROM runtime AS sandbox
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --home-dir /home/sandbox sandbox \
    && mkdir /sandbox \
    && chown sandbox:sandbox /sandbox
USER sandbox
WORKDIR /sandbox
CMD ["robinauts", "sandbox-worker"]
