# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Web's turn routes over a controller on the echo engine and the in-memory store."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from itertools import groupby

import httpx
from test_controller_turns import CONFIG

from aio import asyncio_test
from robinauts.controller.composition import compose
from robinauts.controller.contract.domain import StorageConfig, StorageKind
from robinauts.web.app import create_app


@asynccontextmanager
async def client(
    cancel_ends: bool | None = None,
) -> AsyncIterator[httpx.AsyncClient]:
    composed = compose(CONFIG, storage=StorageConfig(StorageKind.IN_MEMORY), secret_for={}.get)
    if cancel_ends is not None:

        async def cancel_turn(*_: object) -> bool:
            return cancel_ends

        composed.controller.cancel_turn = cancel_turn  # type: ignore[method-assign]
    app = create_app(
        composed.controller, credentials=composed.credentials, sign_in=None, secret_for={}.get
    )
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
        conversation_id = response.headers["x-robinauts-conversation-id"]
        assert run_id != conversation_id
        sent = events(response.text)
        assert (sent[0]["threadId"], sent[0]["runId"]) == (conversation_id, run_id)
        # The echo answers in more than one piece of text.
        assert [t for t, _ in groupby(e["type"] for e in sent)] == [
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


@asyncio_test
async def test_a_turn_is_re_attached_to_by_the_conversation_and_the_run() -> None:
    async with client() as http:
        started = await http.post("/api/turns", json={"agent_id": "echo", "text": "hello"})
        cid = started.headers["x-robinauts-conversation-id"]
        rid = started.headers["x-robinauts-run-id"]
        last = max(
            int(b.removeprefix("id: ")) for b in started.text.split("\n") if b.startswith("id: ")
        )
        opened = (await http.get(f"/api/conversations/{cid}")).json()
        assert opened["run_id"] is None
        assert opened["messages"][1]["provenance"]["run_id"] == rid
        assert opened["messages"][1]["provenance"]["engine"] == "echo"

        replayed = await http.get(f"/api/conversations/{cid}/runs/{rid}/events?after=0")
        assert replayed.headers["x-robinauts-run-id"] == rid
        assert [e["type"] for e in events(replayed.text)][-1] == "RUN_FINISHED"
        at_the_end = await http.get(
            f"/api/conversations/{cid}/runs/{rid}/events", headers={"last-event-id": str(last)}
        )
        assert [e["type"] for e in events(at_the_end.text)] == ["RUN_STARTED", "RUN_FINISHED"]

        elsewhere = await http.get(f"/api/conversations/{cid}/runs/{uuid.uuid4()}/events")
        assert elsewhere.status_code == 404


@asyncio_test
async def test_an_edit_names_the_message_and_a_reply_its_parent() -> None:
    async with client() as http:
        started = await http.post("/api/turns", json={"agent_id": "echo", "text": "hello"})
        cid = started.headers["x-robinauts-conversation-id"]
        question = (await http.get(f"/api/conversations/{cid}")).json()["messages"][0]

        edited = await http.post(
            f"/api/conversations/{cid}/turns", json={"text": "hi", "edit": question["id"]}
        )
        assert events(edited.text)[-1]["type"] == "RUN_FINISHED"
        opened = (await http.get(f"/api/conversations/{cid}")).json()
        assert opened["messages"][0]["parts"] == [{"kind": "text", "text": "hi"}]

        orphan = await http.post(f"/api/conversations/{cid}/turns", json={"text": "more"})
        assert orphan.status_code == 422
        answer = opened["messages"][1]
        not_a_question = await http.post(
            f"/api/conversations/{cid}/turns", json={"text": "x", "edit": answer["id"]}
        )
        assert not_a_question.status_code == 404


@asyncio_test
async def test_an_agent_this_process_does_not_know_is_not_found() -> None:
    async with client() as http:
        response = await http.post("/api/turns", json={"agent_id": "gone", "text": "hello"})
        assert response.status_code == 404
        assert response.json()["error"] == "UnknownAgentError"
        named = await http.post(
            "/api/turns", json={"agent_id": "gone", "model_id": "echo", "text": "hello"}
        )
        assert named.status_code == 404


@asyncio_test
async def test_a_stop_answers_204_when_the_run_ended_and_202_when_its_end_is_on_its_way() -> None:
    url = f"/api/conversations/{uuid.uuid4()}/runs/{uuid.uuid4()}/cancel"
    async with client(cancel_ends=True) as http:
        ended = await http.post(url)
        assert (ended.status_code, ended.content) == (204, b"")
    async with client(cancel_ends=False) as http:
        pending = await http.post(url)
        assert (pending.status_code, pending.content) == (202, b"")


@asyncio_test
async def test_a_stop_of_a_run_that_has_ended_is_not_found() -> None:
    async with client() as http:
        started = await http.post("/api/turns", json={"agent_id": "echo", "text": "hello"})
        cid = started.headers["x-robinauts-conversation-id"]
        rid = started.headers["x-robinauts-run-id"]
        stopped = await http.post(f"/api/conversations/{cid}/runs/{rid}/cancel")
        assert stopped.status_code == 404
        assert stopped.json()["error"] == "NoActiveTurnError"
