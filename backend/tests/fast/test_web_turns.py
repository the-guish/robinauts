# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Web's turn routes over a controller on the echo engine and the in-memory store, beyond
what tests/fast/test_everyday_use.py walks through."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from echo_controller import CONFIG

from robinauts.controller.composition import compose
from robinauts.controller.contract.domain import StorageConfig, StorageKind
from robinauts.web.app import create_app
from util.aio import asyncio_test


@asynccontextmanager
async def client() -> AsyncIterator[httpx.AsyncClient]:
    composed = compose(CONFIG, storage=StorageConfig(StorageKind.IN_MEMORY), secret_for={}.get)
    app = create_app(composed, sign_in=None, secret_for={}.get)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            yield http


def events(body: str) -> list[dict[str, object]]:
    return [
        json.loads(line.removeprefix("data: "))
        for block in body.split("\n\n")
        for line in block.splitlines()
        if line.startswith("data: ")
    ]


@asyncio_test
async def test_a_run_is_re_attached_to_and_a_turn_on_no_question_is_refused() -> None:
    async with client() as http:
        started = await http.post("/api/turns", json={"agent_id": "echo", "text": "hello"})
        cid = started.headers["x-robinauts-conversation-id"]
        rid = started.headers["x-robinauts-run-id"]
        last = max(
            int(b.removeprefix("id: ")) for b in started.text.split("\n") if b.startswith("id: ")
        )

        replayed = await http.get(f"/api/conversations/{cid}/runs/{rid}/events?after=0")
        assert replayed.headers["x-robinauts-run-id"] == rid
        assert [e["type"] for e in events(replayed.text)][-1] == "RUN_FINISHED"
        at_the_end = await http.get(
            f"/api/conversations/{cid}/runs/{rid}/events", headers={"last-event-id": str(last)}
        )
        assert [e["type"] for e in events(at_the_end.text)] == ["RUN_STARTED", "RUN_FINISHED"]
        elsewhere = await http.get(f"/api/conversations/{cid}/runs/{uuid.uuid4()}/events")
        assert elsewhere.status_code == 404

        # A message names the parent it answers or the question it edits.
        orphan = await http.post(f"/api/conversations/{cid}/turns", json={"text": "more"})
        assert orphan.status_code == 422
        answer = (await http.get(f"/api/conversations/{cid}")).json()["messages"][1]
        not_a_question = await http.post(
            f"/api/conversations/{cid}/turns", json={"text": "x", "edit": answer["id"]}
        )
        assert not_a_question.status_code == 404
