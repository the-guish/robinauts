#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors
#
# The backend test suite, including the architecture contracts of
# docs/layout.md. Further arguments are passed on to pytest.
#
# ROBINAUTS_TEST_DATABASE_URL, if it is set in the environment, is the
# PostgreSQL the tests under backend/tests/adapters_free/ use; they create and
# drop a schema of their own in it. Without it those tests skip and the rest
# of the suite runs as it always does, so this script needs no argument and
# no mode: CONTRIBUTING.md says how to set it.
set -eu

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$root/backend"

# conftest.py stops bytecode being written, but not its own: pytest imports it
# before it can say so. A stray __pycache__ in the tree is what `reuse lint`
# then reads, so nothing is written at all here.
PYTHONDONTWRITEBYTECODE=1
export PYTHONDONTWRITEBYTECODE

# One pytest-xdist worker per core, and no more than 4. `loadfile` keeps a module on one
# worker, so that a module that starts a server of its own starts it once; each worker
# starts its own shared server for tests/api, on a database of its own
# (tests/util/shared_server.py).
#
# The cap is PostgreSQL's: a server holds about 10 connections, and a stock PostgreSQL
# allows 100. Four workers peak near 80; sixteen reach the limit, and servers die at their
# start with "too many clients already". 4 is also what CI's runner has.
uv run --locked --no-build pytest -n auto --maxprocesses 4 --dist loadfile "$@"
