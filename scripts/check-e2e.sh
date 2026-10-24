#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors
#
# The interface in a real browser: backend/tests/e2e/, which a plain test run
# does not collect. It builds the interface, installs Playwright's Chromium and
# runs the tests against a real server on every agent engine, over a model
# server and an MCP server the tests run on this machine, so none needs a key.
# Slower than the rest of the suite, so CI runs it after the fast tests.
# Further arguments are passed on to pytest.
#
# The server stores in the PostgreSQL ROBINAUTS_TEST_DATABASE_URL names, in a
# schema the tests create and drop, as for the database tests
# (CONTRIBUTING.md). Without it, off CI, this starts a throwaway one in Docker
# and removes it when it is done.
set -eu

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)

# nvm, if this machine has it, for the Node of frontend/.nvmrc; not on CI,
# where setup-node has already put it on the PATH (scripts/check-frontend.sh).
if [ -z "${CI:-}" ] && [ -z "${ROBINAUTS_NODE_CHOSEN:-}" ] &&
    [ -s "$HOME/.nvm/nvm.sh" ] && command -v bash >/dev/null 2>&1; then
    ROBINAUTS_NODE_CHOSEN=1
    export ROBINAUTS_NODE_CHOSEN
    exec bash -c 'cd "$1/frontend" && . "$HOME/.nvm/nvm.sh" >/dev/null 2>&1 &&
        nvm use >/dev/null 2>&1 && shift && exec "$@"' \
        bash "$root" "$root/scripts/check-e2e.sh" "$@"
fi

# The image the `test` job of .github/workflows/ci.yml pins: PostgreSQL 16.15.
postgres_image=postgres@sha256:a3b7f434b2dc57ce85a67e171163eb8ab1a1ebcb39d27484661f26b1dfbe30d6

# Started before the build, so that it is up by the time the tests need it.
# Published on a free loopback port Docker picks.
if [ -z "${ROBINAUTS_TEST_DATABASE_URL:-}" ] && [ -z "${CI:-}" ]; then
    container=$(docker run --detach --rm \
        --env POSTGRES_PASSWORD=robinauts-test --env POSTGRES_DB=robinauts_test \
        --env TZ=Asia/Kathmandu --publish 127.0.0.1::5432 "$postgres_image")
    trap 'docker rm --force "$container" >/dev/null' EXIT
    trap 'exit 130' INT TERM
    port=$(docker port "$container" 5432/tcp | head -n 1 | sed 's/.*://')
    ROBINAUTS_TEST_DATABASE_URL=postgresql://postgres:robinauts-test@127.0.0.1:$port/robinauts_test
    export ROBINAUTS_TEST_DATABASE_URL
fi

# The interface the server serves, built from the lock. CHECK_BUNDLED keeps
# the build from rewriting bundled-packages.txt.
cd "$root/frontend"
npm ci --ignore-scripts
CHECK_BUNDLED=1 npm run build

cd "$root/backend"
PYTHONDONTWRITEBYTECODE=1
export PYTHONDONTWRITEBYTECODE

# The Chromium of the locked Playwright; on CI, with the system libraries it
# needs, which the runner may lack.
uv run --locked --no-build playwright install ${CI:+--with-deps} chromium

uv run --locked --no-build python ../scripts/wait_for_postgres.py
uv run --locked --no-build pytest tests/e2e "$@"
