#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors
#
# The dependency licence gate: the policy of DEPENDENCIES.md over the whole
# locked set of backend/uv.lock, then over each extra worker's lock
# (extra-workers/*/uv.lock) and each MCP server's (mcp/*/uv.lock), each read
# against its own synced environment.
# Further arguments are passed on to the gate; see scripts/licence_gate.py --help.
set -eu

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$root/backend"

# Run under the synced environment: the gate reads the licence metadata of
# what is installed, and asks PyPI only about what this platform leaves out.
uv run --locked --no-build python "$root/scripts/licence_gate.py" "$@"

for project in "$root"/extra-workers/*/; do
    [ -f "$project/uv.lock" ] || continue
    name=$(basename "$project")
    printf '\n== %s ==\n' "$name"
    # Synced first, so the gate reads its metadata from what is installed. The gate
    # reads third-party metadata only, so the local projects stay out and nothing is built.
    (cd "$project" && uv sync --locked --no-build --no-install-local)
    uv run --locked --no-build python "$root/scripts/licence_gate.py" --extra-worker "$name" "$@"
done

for project in "$root"/mcp/*/; do
    [ -f "$project/uv.lock" ] || continue
    name=$(basename "$project")
    printf '\n== mcp/%s ==\n' "$name"
    # As for an extra worker: synced first, the server's own package left out, nothing built.
    (cd "$project" && uv sync --locked --no-build --no-install-local)
    uv run --locked --no-build python "$root/scripts/licence_gate.py" --mcp-server "$name" "$@"
done
