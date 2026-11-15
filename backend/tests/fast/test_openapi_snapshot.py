# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The committed OpenAPI document, kept in step with web's routes.

``backend/openapi.json`` is a snapshot (``docs/specs/backend.md``): the
interface's typed client is generated from it, so a change to the wire has to
be visible in a diff rather than only in a running server. This fails when the
file and the code disagree, and says what to run.
"""

from __future__ import annotations

import json
from pathlib import Path

from robinauts.cli.main import openapi_document

SNAPSHOT = Path(__file__).resolve().parents[2] / "openapi.json"

REGENERATE = "scripts/update-openapi.sh"


def written() -> str:
    """The snapshot's one spelling, the one ``scripts/update-openapi.sh`` writes."""
    return json.dumps(openapi_document(), indent=2, sort_keys=True) + "\n"


def test_the_snapshot_is_what_the_code_describes() -> None:
    assert (
        SNAPSHOT.read_text(encoding="utf-8") == written()
    ), f"the routes and {SNAPSHOT.name} disagree; run {REGENERATE} and read the diff"


def test_the_streaming_routes_are_outside_the_document() -> None:
    """They answer server-sent events, not JSON (``docs/specs/wire.md``)."""
    paths = json.loads(SNAPSHOT.read_text(encoding="utf-8"))["paths"]

    assert "/robinauts/api/turns" not in paths
    assert "/robinauts/api/conversations/{conversation_id}/turns" not in paths
    assert "/robinauts/api/conversations/{conversation_id}/runs/{run_id}/events" not in paths
    assert "/robinauts/api/conversations" in paths
