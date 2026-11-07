#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors
#
# The dependency licence gate: the policy of DEPENDENCIES.md over the whole
# locked set of backend/uv.lock, and then over each extra worker's lock
# (extra-workers/*/uv.lock), each read against its own synced environment.
# Further arguments are passed on to the gate; see scripts/licence_gate.py --help.
set -eu

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$root/backend"

# Run under the synced environment: the gate reads the licence metadata of
# what is installed, and asks PyPI only about what this platform leaves out.
uv run --locked --no-build python "$root/scripts/licence_gate.py" "$@"

for project in "$root"/extra-workers/*/; do
    [ -f "$project/uv.lock" ] || continue
    printf '\n== %s ==\n' "$(basename "$project")"
    # Synced first, so the gate reads its metadata from what is installed. The backend
    # is installed editable there, which builds its metadata: no --no-build.
    (cd "$project" && uv sync --locked)
    uv run --locked --no-build python "$root/scripts/licence_gate.py" \
        --lock "$project/uv.lock" --environment "$project/.venv" "$@"
done
