#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors
#
# Write backend/openapi.json from the routes as they are now.
#
# The document is a committed snapshot (docs/specs/backend.md): the frontend's
# typed client is generated from it, and a reviewer sees a change to the wire
# in the diff rather than in a running server. `test_openapi_snapshot.py` fails
# when the file and the code disagree, and this is what makes them agree again.
set -eu

if [ "$#" -ne 0 ]; then
    printf '%s takes no arguments (got: %s)\n' "$0" "$*" >&2
    exit 2
fi

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$root/backend"

PYTHONDONTWRITEBYTECODE=1
export PYTHONDONTWRITEBYTECODE

uv run --locked python - <<'PY'
import json
from pathlib import Path

from robinauts.controller.contract.domain import Config, StorageConfig, StorageKind
from robinauts.controller.composition import compose
from robinauts.web.app import create_app

composed = compose(
    Config(), storage=StorageConfig(StorageKind.IN_MEMORY), secret_for={}.get
)
app = create_app(
    composed.controller,
    credentials=composed.credentials,
    sign_in=None,
    secret_for={}.get,
)
document = app.openapi()
snapshot = Path("openapi.json")
snapshot.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
print(f"wrote {snapshot.resolve()}")
PY
