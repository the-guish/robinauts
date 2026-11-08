#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors
#
# ruff and black, over the backend and over the Python of this repository's own
# scripts: scripts/ and demo/.
set -eu

if [ "$#" -ne 0 ]; then
    printf '%s takes no arguments (got: %s)\n' "$0" "$*" >&2
    exit 2
fi

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$root/backend"

# backend/pyproject.toml holds the only ruff and black configuration there is.
# The scripts live outside it, so the configuration is named explicitly rather
# than discovered, and they are checked under the same rules as the backend.
# demo/ is here for the same reason: demo/pg.py is Python of ours, and a demo
# nobody lints is a demo that rots.
uv run --locked --no-build ruff check --config pyproject.toml . ../scripts ../demo ../orchestration
uv run --locked --no-build black --check --config pyproject.toml . ../scripts ../demo ../orchestration
