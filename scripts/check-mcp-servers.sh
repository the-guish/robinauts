#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors
#
# The MCP servers of this repository (mcp/README.md): each is a Python project of
# its own, which stands alone. For each, in turn: its locked environment, ruff
# and black under its own configuration (the backend's rules, copied), and its
# tests. Their licences are checked by scripts/check-licences.sh.
set -eu

if [ "$#" -ne 0 ]; then
    printf '%s takes no arguments (got: %s)\n' "$0" "$*" >&2
    exit 2
fi

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)

# As scripts/check-tests.sh: nothing writes bytecode into the tree.
PYTHONDONTWRITEBYTECODE=1
export PYTHONDONTWRITEBYTECODE

for project in "$root"/mcp/*/; do
    [ -f "$project/pyproject.toml" ] || continue
    printf '\n== mcp/%s ==\n' "$(basename "$project")"
    cd "$project"
    uv sync --locked
    uv run --locked ruff check .
    uv run --locked black --check .
    uv run --locked pytest -q -p no:cacheprovider
done
