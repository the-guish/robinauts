# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The committed OpenAPI document, kept in step with the routes.

``backend/openapi.json`` is a snapshot (``docs/specs/backend.md``): the
interface's typed client is generated from it, so a change to the wire has to
be visible in a diff rather than only in a running server. This fails when the
file and the code disagree, and says what to run.

It compares the file's **text**, not only the document it parses to, because
the file is read by people and by a generator: two files that mean the same
thing and are written differently are still a diff nobody asked for.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import get_args, get_origin

from robinauts.legacy.api import (
    ANYTHING_ELSE,
    NewChatRequest,
    RenameRequest,
    TurnRequest,
    openapi_document,
)

REQUESTS = (RenameRequest, NewChatRequest, TurnRequest)
"""Every body a route of the API reads. Written out, like ``ANSWERS``.

The last two are the streaming routes', which are outside the document
(``docs/specs/wire.md``) -- and the rule about what a refusal may name is about
what pydantic is given, not about what OpenAPI describes, so they are held to
it like the rest.
"""

ANSWERS = (
    "AgentListResponse",
    "AgentSummary",
    "ConversationListResponse",
    "ConversationSummary",
    "EndedBadlyView",
    "MessageView",
    "OpenedConversationResponse",
    "ProvenanceView",
    "ResumeView",
    "RunView",
    "TextContent",
    "ToolCallContent",
    "ToolResultContent",
)
"""Every shape the conversation and agent routes answer with.

Written out rather than derived: a shape added to the wire is then a line in
this file and something a reviewer reads, which is the same reason the
declaration table in ``test_api_access.py`` is written out.
"""

SNAPSHOT = Path(__file__).resolve().parents[2] / "openapi.json"

REGENERATE = "scripts/update-openapi.sh"


def written(document: dict[str, object]) -> str:
    """The snapshot's one spelling: two-space indent, keys in order, newline.

    The same spelling ``scripts/update-openapi.sh`` writes. If the two ever
    part company, this test is what says so.
    """
    return json.dumps(document, indent=2, sort_keys=True) + "\n"


def test_the_snapshot_is_what_the_code_describes() -> None:
    assert SNAPSHOT.exists(), f"{SNAPSHOT} is missing; run {REGENERATE}"

    assert SNAPSHOT.read_text(encoding="utf-8") == written(
        openapi_document()
    ), f"the routes and {SNAPSHOT.name} disagree; run {REGENERATE} and read the diff"


def test_the_snapshot_describes_the_routes_a_client_is_generated_for() -> None:
    """The browser navigations are out; everything a client calls is in."""
    document = json.loads(SNAPSHOT.read_text(encoding="utf-8"))

    assert sorted(document["paths"]) == [
        "/api/agents",
        "/api/conversations",
        "/api/conversations/{conversation_id}",
        "/api/conversations/{conversation_id}/model",
        "/api/conversations/{conversation_id}/runs/{run_id}/cancel",
        "/api/models",
        "/auth/logout",
        "/auth/session",
        "/health",
    ]
    assert "/auth/login/{provider}" not in document["paths"]
    assert "/auth/callback/{provider}" not in document["paths"]


def test_the_document_says_how_every_refusal_is_shaped() -> None:
    """Ours, not the framework's.

    FastAPI describes the 422 of a route with parameters in a shape of its
    own (``HTTPValidationError``), and this project answers every refusal in
    one shape of its own (``robinauts.legacy.api.errors``). A client generated from a
    document that said otherwise would fail to read the body it really gets,
    so every route names the statuses it can refuse with
    (``robinauts.legacy.api.conversation_routes.refusals``).
    """
    document = json.loads(SNAPSHOT.read_text(encoding="utf-8"))

    assert "HTTPValidationError" not in document["components"]["schemas"]
    refused = {
        (path, method, status): answer.get("content", {})
        .get("application/json", {})
        .get("schema", {})
        .get("$ref")
        for path, methods in document["paths"].items()
        for method, operation in methods.items()
        for status, answer in operation["responses"].items()
        if status == "default" or int(status) >= 400
    }
    assert refused, "no route describes a refusal"
    assert set(refused.values()) == {"#/components/schemas/ErrorResponse"}


def test_every_route_of_the_api_describes_the_refusals_it_cannot_list() -> None:
    """The 405 and the 500 no route decides, described once as the fallback.

    Any route can be asked with the wrong method and any can meet a bug, so
    they are the document's ``default`` answer rather than two more rows on
    every route (``robinauts.legacy.api.refusals.ANYTHING_ELSE``).
    """
    document = json.loads(SNAPSHOT.read_text(encoding="utf-8"))

    for path, methods in document["paths"].items():
        if not path.startswith("/api/"):
            continue
        for method, operation in methods.items():
            assert "default" in operation["responses"], f"{method} {path}"
            assert operation["responses"]["default"]["description"] == ANYTHING_ELSE


def test_no_request_model_puts_a_senders_word_in_a_location() -> None:
    """What a refusal names is a field of ours, and that has to stay true.

    ``api.errors`` builds the detail of an unreadable request out of the
    **location** pydantic gives, keeping the pieces that are text -- and that
    is a rule about the type of a piece, not about where it came from. Two
    things would put a sender's own word in one: a field typed as a
    **mapping**, whose keys are theirs, and a **discriminated union**, whose
    tag pydantic writes into the location. Neither exists; whoever writes the
    first one meets this test and the paragraph on ``errors._located``.
    """
    for model in REQUESTS:
        for name, field in model.model_fields.items():
            assert field.discriminator is None, f"{model.__name__}.{name}"
            assert not _mapping_inside(field.annotation), f"{model.__name__}.{name}"


def _mapping_inside(annotation: object) -> bool:
    """Whether a mapping is anywhere inside that annotation."""
    origin = get_origin(annotation)
    if origin is not None and isinstance(origin, type) and issubclass(origin, Mapping):
        return True
    return any(_mapping_inside(inside) for inside in get_args(annotation))


def test_every_field_of_an_answer_is_always_there() -> None:
    """A field that is sent every time is ``required``, and ``null`` when empty.

    A client generated from a schema whose fields are optional has to ask
    whether each one is present *and* whether it is null, for an answer that
    always carries all of them. So the shapes this step added declare every
    field required, and say ``T | null`` where there may be nothing.

    ``SessionResponse``, ``UserSummary``, ``ProviderSummary`` and
    ``HealthResponse`` predate the rule and are deliberately not in the list:
    changing them is a change to the sign-in step's wire.
    """
    document = json.loads(SNAPSHOT.read_text(encoding="utf-8"))

    for name in ANSWERS:
        schema = document["components"]["schemas"][name]
        assert set(schema.get("properties", {})) == set(schema.get("required", [])), name


def test_the_snapshot_names_no_host() -> None:
    """It describes the routes, not a deployment.

    A ``servers`` entry would bake somebody's public URL into a committed
    file, and into every client generated from it.
    """
    document = json.loads(SNAPSHOT.read_text(encoding="utf-8"))

    assert "servers" not in document
    assert document["info"]["version"] == "0"
