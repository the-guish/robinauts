#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors
#
# The desktop with Docker (docs/deployment.md, "The desktop with Docker"): Robinauts' image,
# built from the Dockerfile at the root, and the base worker's, then the tests that start the
# desktop on the Docker daemon with both (backend/tests/api/test_desktop_docker.py). A plain
# test run skips those, since the images take minutes to build.
#
# ROBINAUTS_TEST_IMAGE and ROBINAUTS_BASE_WORKER_IMAGE, when set, name images already built,
# and this builds neither. Further arguments are passed on to pytest.
set -eu

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)

if ! docker info >/dev/null 2>&1; then
    printf 'Docker is not running here: the desktop with Docker needs it\n' >&2
    exit 1
fi

if [ -z "${ROBINAUTS_TEST_IMAGE:-}" ]; then
    ROBINAUTS_TEST_IMAGE=robinauts:check
    docker build --quiet -f "$root/Dockerfile" -t "$ROBINAUTS_TEST_IMAGE" "$root"
fi
if [ -z "${ROBINAUTS_BASE_WORKER_IMAGE:-}" ]; then
    ROBINAUTS_BASE_WORKER_IMAGE=robinauts-base-worker:check
    docker build --quiet -f "$root/extra-workers/base-worker/Dockerfile" \
        -t "$ROBINAUTS_BASE_WORKER_IMAGE" "$root"
fi
# A test that would skip here fails instead: this check exists to run them.
ROBINAUTS_REQUIRE_DOCKER=1
export ROBINAUTS_TEST_IMAGE ROBINAUTS_BASE_WORKER_IMAGE ROBINAUTS_REQUIRE_DOCKER

# As scripts/check-tests.sh: nothing writes bytecode into the tree.
PYTHONDONTWRITEBYTECODE=1
export PYTHONDONTWRITEBYTECODE

cd "$root/backend"
uv run --locked --no-build pytest -p no:cacheprovider -m docker tests/api/test_desktop_docker.py "$@"
