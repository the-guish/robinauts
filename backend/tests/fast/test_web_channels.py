# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The channels endpoint (``web/channels.py``) over a controller on the echo engine and the
in-memory store: AG-UI's own run input from a bridge, for the platform user it names."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from echo_controller import CONFIG

from robinauts.controller.composition import Composed, compose
from robinauts.controller.contract.domain import (
    ConfigError,
    Identity,
    MessageCompleted,
    MessageStarted,
    Role,
    StorageConfig,
    StorageKind,
    TextPiece,
    TurnEnded,
    TurnState,
)
from robinauts.web import agui
from robinauts.web.app import LOCAL_IDENTITY, create_app
from robinauts.web.channels import CHANNEL_PROVIDER, ChannelsConfig, channels_config, question_of
from util.aio import asyncio_test

SECRET = "s" * 40
BRIDGE = {"authorization": f"Bearer {SECRET}"}
CHANNELS = ChannelsConfig(SECRET)
ECHO = "/api/channels/agents/echo/agui"


@asynccontextmanager
async def served(
    channels: ChannelsConfig | None = CHANNELS,
) -> AsyncIterator[tuple[httpx.AsyncClient, Composed]]:
    composed = compose(CONFIG, storage=StorageConfig(StorageKind.IN_MEMORY), secret_for={}.get)
    app = create_app(composed, sign_in=None, secret_for={}.get, channels=channels)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            yield http, composed


def run_input(
    text: str, *, thread: str = "tg-thread-1", user: str = "telegram:t:42", **extra: Any
) -> dict[str, Any]:
    """What a stock AG-UI client sends: the history it holds, and the user in forwardedProps."""
    return {
        "threadId": thread,
        "runId": "client-run",
        "state": {},
        "messages": [
            {"id": "m1", "role": "user", "content": "an earlier question"},
            {"id": "m2", "role": "assistant", "content": "an earlier answer"},
            {"id": "m3", "role": "user", "content": text},
        ],
        "tools": [{"name": "lookup_user", "description": "", "parameters": {}}],
        "context": [{"description": "a hint", "value": "ignored"}],
        "forwardedProps": {"robinauts": {"user": {"id": user, "name": "Ada"}}},
        **extra,
    }


def events(body: str) -> list[dict[str, Any]]:
    return [
        json.loads(line.removeprefix("data: "))
        for block in body.split("\n\n")
        for line in block.splitlines()
        if line.startswith("data: ")
    ]


@asyncio_test
async def test_a_new_thread_starts_a_conversation_and_its_id_continues_it() -> None:
    async with served() as (http, composed):
        first = await http.post(ECHO, json=run_input("hello"), headers=BRIDGE)
        assert first.status_code == 200
        conversation = first.headers["x-robinauts-conversation-id"]
        sent = events(first.text)
        assert [sent[0]["type"], sent[-1]["type"]] == ["RUN_STARTED", "RUN_FINISHED"]
        # The thread is the conversation; the run is the one the client chose.
        assert (sent[0]["threadId"], sent[0]["runId"]) == (conversation, "client-run")
        assert "TEXT_MESSAGE_CONTENT" in {e["type"] for e in sent}

        again = await http.post(
            ECHO, json=run_input("and again", thread=conversation), headers=BRIDGE
        )
        assert again.status_code == 200
        assert again.headers["x-robinauts-conversation-id"] == conversation

        user = await composed.controller.ensure_user(
            Identity(provider=CHANNEL_PROVIDER, subject="telegram:t:42")
        )
        opened = await composed.controller.open_session(user, uuid.UUID(conversation))
        # Only the last user message of each input was asked: the history is the store's.
        questions = [m.parts[0].text for m in opened.messages if m.role is Role.USER]
        assert questions == ["hello", "and again"]
        assert user.name == "Ada"


@asyncio_test
async def test_the_bridge_proves_itself_with_the_shared_secret() -> None:
    async with served() as (http, _):
        nobody = await http.post(ECHO, json=run_input("hello"))
        wrong = await http.post(
            ECHO, json=run_input("hello"), headers={"authorization": "Bearer " + "x" * 40}
        )
        assert (nobody.status_code, wrong.status_code) == (401, 401)
        # Refused before the body is read: a stranger learns nothing of its shape.
        assert (await http.post(ECHO, json={})).status_code == 401
    async with served(channels=None) as (http, _):
        assert (await http.post(ECHO, json=run_input("hi"), headers=BRIDGE)).status_code == 404


@asyncio_test
async def test_a_bridge_reaches_only_the_conversations_of_the_user_it_names() -> None:
    async with served() as (http, composed):
        started = await http.post(ECHO, json=run_input("mine"), headers=BRIDGE)
        conversation = started.headers["x-robinauts-conversation-id"]
        theirs = await http.post(
            ECHO, json=run_input("yours?", thread=conversation, user="telegram:t:7"), headers=BRIDGE
        )
        assert theirs.status_code == 404

        # Nor one of somebody who signs in, here the local development mode's user.
        local = await composed.controller.ensure_user(LOCAL_IDENTITY)
        web = await composed.controller.start_session(local, agent="echo", model="echo", text="x")
        assert web.session_id is not None
        reached = await http.post(
            ECHO, json=run_input("hi", thread=str(web.session_id)), headers=BRIDGE
        )
        assert reached.status_code == 404


@asyncio_test
async def test_what_a_channel_turn_refuses() -> None:
    async with served() as (http, _):
        unknown = await http.post(
            "/api/channels/agents/nobody/agui", json=run_input("hi"), headers=BRIDGE
        )
        assert unknown.status_code == 404
        empty = await http.post(ECHO, json=run_input("  "), headers=BRIDGE)
        assert empty.status_code == 422
        anonymous = run_input("hi")
        anonymous["forwardedProps"] = {}
        assert (await http.post(ECHO, json=anonymous, headers=BRIDGE)).status_code == 422
        bad_model = run_input("hi")
        bad_model["forwardedProps"]["robinauts"]["model_id"] = "nope"
        assert (await http.post(ECHO, json=bad_model, headers=BRIDGE)).status_code == 422


def test_the_question_is_the_last_user_message_and_its_text_parts() -> None:
    assert question_of([]) == ""
    assert question_of([{"role": "user", "content": "a"}, {"role": "assistant"}]) == "a"
    parts = [
        {"type": "text", "text": "look"},
        {"type": "image", "source": {"type": "data", "value": "", "mimeType": "image/png"}},
        {"type": "text", "text": "here"},
    ]
    assert question_of([{"role": "user", "content": parts}]) == "look\nhere"


def test_the_secret_is_read_from_the_environment_and_must_be_long() -> None:
    assert channels_config({}) is None
    assert channels_config({"ROBINAUTS_CHANNELS_SECRET": SECRET}) == ChannelsConfig(SECRET)
    with pytest.raises(ConfigError):
        channels_config({"ROBINAUTS_CHANNELS_SECRET": "short"})
    with pytest.raises(ConfigError):
        channels_config({"ROBINAUTS_CHANNELS_SECRET": SECRET + " "})
    assert not ChannelsConfig(SECRET).admits(SECRET)
    assert ChannelsConfig(SECRET).admits(f"bearer {SECRET}")


def test_a_stock_client_is_told_of_a_cancellation_as_an_error() -> None:
    """AG-UI before 1.0 has no cancelled outcome, and refuses a RUN_FINISHED that names one."""
    cancelled = TurnEnded(state=TurnState.CANCELLED)
    web = agui.mapped("t", "r", cancelled)
    stock = agui.mapped("t", "r", cancelled, stock=True)
    assert web[0].type.value == "RUN_FINISHED"
    assert (stock[0].type.value, stock[0].code) == ("RUN_ERROR", "cancelled")
    message = uuid.uuid4()
    for event in (
        MessageStarted(message, parent_id=uuid.uuid4()),
        TextPiece(message, "x"),
        MessageCompleted(message),
    ):
        assert agui.mapped("t", "r", event) == agui.mapped("t", "r", event, stock=True)
