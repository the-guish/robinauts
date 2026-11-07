#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors
#
# The extra workers (extra-workers/README.md): each is a Python project of its
# own, composed from the backend beside it. For each, in turn: its locked
# environment, ruff and black under its own configuration (the backend's rules,
# copied), its tests, and its image, built where Docker is there to build it.
# Then the one rule the backend's import-linter cannot see: nothing under
# backend/src names an extra worker.
set -eu

if [ "$#" -ne 0 ]; then
    printf '%s takes no arguments (got: %s)\n' "$0" "$*" >&2
    exit 2
fi

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)

# As scripts/check-tests.sh: nothing writes bytecode into the tree.
PYTHONDONTWRITEBYTECODE=1
export PYTHONDONTWRITEBYTECODE

for project in "$root"/extra-workers/*/; do
    [ -f "$project/pyproject.toml" ] || continue
    name=$(basename "$project")
    printf '\n== %s ==\n' "$name"
    cd "$project"
    # The backend is installed editable, which builds its metadata: no --no-build.
    uv sync --locked
    # The project's own files: its tests, and its package where it has one.
    uv run --locked ruff check .
    uv run --locked black --check .
    uv run --locked pytest -q -p no:cacheprovider
    if [ -f "$project/Dockerfile" ]; then
        if docker info >/dev/null 2>&1; then
            printf '\n-- the image --\n'
            docker build --quiet -f "$project/Dockerfile" -t "robinauts-$name:check" "$root"
        else
            printf '\n-- the image: not built, Docker is not running here; CI builds it --\n'
        fi
    fi
done

printf '\n== backend/src names no extra worker ==\n'
if grep -rn "robinauts_claude_worker\|claude_agent_sdk\|extra_workers" "$root/backend/src"; then
    printf 'backend/src names an extra worker: the layers are the other way round\n' >&2
    exit 1
fi
printf 'none\n'
