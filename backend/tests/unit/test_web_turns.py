# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Web's turn routes over a controller on the echo engine and the in-memory store."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from itertools import groupby

import httpx
from test_controller_turns import CONFIG

from aio import asyncio_test
from robinauts.controller.contract.domain import StorageConfig, StorageKind
from robinauts.controller.controller import RobinautsController
from robinauts.web.app import create_app


@asynccontextmanager
async def client() -> AsyncIterator[httpx.AsyncClient]:
    controller = RobinautsController(
        CONFIG, storage=StorageConfig(StorageKind.IN_MEMORY), secret_for={}.get
    )
    app = create_app(controller)
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
async def test_a_new_conversation_streams_the_turn_as_agui_events() -> None:
    async with client() as http:
        response = await http.post("/api/turns", json={"agent_id": "echo", "text": "hello"})
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        run_id = response.headers["x-robinauts-run-id"]
        assert response.headers["x-robinauts-conversation-id"] == run_id
        # The echo answers in more than one piece of text.
        assert [t for t, _ in groupby(e["type"] for e in events(response.text))] == [
            "RUN_STARTED",
            "TEXT_MESSAGE_START",
            "TOOL_CALL_START",
            "TOOL_CALL_ARGS",
            "TOOL_CALL_END",
            "TOOL_CALL_RESULT",
            "TEXT_MESSAGE_CONTENT",
            "TEXT_MESSAGE_END",
            "RUN_FINISHED",
        ]


@asyncio_test
async def test_the_conversation_then_shows_the_question_and_the_answer() -> None:
    async with client() as http:
        started = await http.post("/api/turns", json={"agent_id": "echo", "text": "hello"})
        cid = started.headers["x-robinauts-conversation-id"]
        opened = (await http.get(f"/api/conversations/{cid}")).json()
        assert opened["conversation"]["model"] == "echo"
        assert opened["run_id"] is None
        assert [m["role"] for m in opened["messages"]] == ["user", "assistant"]
        assert opened["messages"][0]["parts"] == [{"kind": "text", "text": "hello"}]
        assert [p["kind"] for p in opened["messages"][1]["parts"]] == [
            "tool_call",
            "tool_result",
            "text",
        ]

        again = await http.post(
            f"/api/conversations/{cid}/turns",
            json={"text": "more", "parent_id": opened["messages"][1]["id"]},
        )
        assert events(again.text)[-1]["type"] == "RUN_FINISHED"
        opened = (await http.get(f"/api/conversations/{cid}")).json()
        assert len(opened["messages"]) == 4
