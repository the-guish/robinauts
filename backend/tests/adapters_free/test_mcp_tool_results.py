# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""An MCP tool's result, on both engines, from a real MCP server: the model is sent its text,
and the engine reports its structured content apart (``docs/architecture/mcp-apps.md``).

The engines' own MCP clients make the call; only the model is a stand-in, which keeps what it
was sent (``chat_completions.Vendor``).

The structured content is for a view, and the transcript keeps it. The Pydantic AI engine's
session, which holds a tool result as the framework has it, keeps none of it.
"""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import Callable
from typing import Any

import pytest
from mcp.types import CallToolResult, TextContent
from pydantic_ai.messages import ModelMessage, ModelRequest, ToolReturnPart
from test_engines_over_chat_completions import SETTINGS
from test_tool_server_exclude import Build, langchain, pydantic_ai

from robinauts.agent_engines.contract.domain import (
    AgentDefinition,
    Event,
    ToolResult,
    ToolServerAuth,
    ToolServerConfig,
)
from robinauts.agent_engines.contract.ports import AgentEngine, EngineSettings
from robinauts.agent_engines.pydantic_ai_engine.engine import PydanticAIEngine
from robinauts.agent_engines.pydantic_ai_engine.session_store import InProcessSessionStore
from util.aio import asyncio_test
from util.chat_completions import Vendor, calling, finished, said, streamed
from util.contracts.engine import ARGUMENTS, CALL_ID, add
from util.mcp_server import mcp_server

pytestmark = pytest.mark.io

DRAWN = "A diagram of two boxes, a and b."
LAYOUT = {"nodes": [{"id": "a", "x": 0}, {"id": "b", "x": 120}], "revision": 1}


def draw() -> CallToolResult:
    """Words for the model, and a layout for a view."""
    return CallToolResult(content=[TextContent(type="text", text=DRAWN)], structuredContent=LAYOUT)


def words() -> CallToolResult:
    """Words alone."""
    return CallToolResult(content=[TextContent(type="text", text=DRAWN)])


def text_of(content: str | list[dict[str, Any]]) -> str:
    """A tool message's text, which a framework sends as one string or as text parts."""
    if isinstance(content, str):
        return content
    assert all(part["type"] == "text" for part in content)
    return "".join(part["text"] for part in content)


async def round_of(
    build: Build, tool: Callable[..., Any], arguments: dict[str, Any], mp: pytest.MonkeyPatch
) -> tuple[list[Event], Vendor]:
    """One turn in which the model calls ``tool`` of a real MCP server, then answers."""
    with mcp_server(tool) as url:
        server = ToolServerConfig("s", url, auth=ToolServerAuth.NONE)
        models = dataclasses.replace(SETTINGS.models, tool_servers={"s": server})
        vendor = Vendor(
            bodies=[
                streamed(*calling(CALL_ID, tool.__name__, arguments), *finished("tool_calls")),
                streamed(*said("ok"), *finished()),
            ]
        )
        engine = build(vendor, dataclasses.replace(SETTINGS, models=models), mp)
        await engine.setup()
        session = uuid.uuid4()
        await engine.create(session)
        agent = AgentDefinition("be brief", tools=("s",))
        stream = engine.stream(
            session, agent, "hi", model="m", checkpoint_id=None, timeout_seconds=10.0
        )
        return [event async for event in stream], vendor


@pytest.mark.parametrize("build", [langchain, pydantic_ai], ids=["langchain", "pydantic-ai"])
@pytest.mark.parametrize(
    ("tool", "arguments", "text", "structured"),
    [
        (draw, {}, DRAWN, LAYOUT),
        (words, {}, DRAWN, None),
        # A typed return: FastMCP writes it as text, and as structured content under `result`.
        (add, ARGUMENTS, "5", {"result": 5}),
    ],
    ids=["text-and-structured", "text-alone", "typed-return"],
)
@asyncio_test
async def test_the_model_is_sent_the_text_and_the_structured_content_is_reported_apart(
    build: Build,
    tool: Callable[..., Any],
    arguments: dict[str, Any],
    text: str,
    structured: dict[str, Any] | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events, vendor = await round_of(build, tool, arguments, monkeypatch)

    [result] = [event for event in events if isinstance(event, ToolResult)]
    assert result == ToolResult(CALL_ID, tool.__name__, text, False, structured)
    _, second = vendor.sent
    sent = Vendor.body_of(second)["messages"][-1]
    assert (sent["role"], sent["tool_call_id"]) == ("tool", CALL_ID)
    assert text_of(sent["content"]) == text


class EverySave(InProcessSessionStore):
    """A session store that also keeps each history it was asked to save: a turn saves after
    every tool round, and each save takes the place of the one before."""

    def __init__(self) -> None:
        super().__init__()
        self.saved: list[list[ModelMessage]] = []

    async def save(
        self, session_id: uuid.UUID, checkpoint_id: str, messages: list[ModelMessage]
    ) -> None:
        self.saved.append(list(messages))
        await super().save(session_id, checkpoint_id, messages)


def pydantic_ai_on(store: InProcessSessionStore) -> Build:
    """The Pydantic AI engine over ``store``, its model plugged as ``pydantic_ai`` plugs it."""

    def build(vendor: Vendor, settings: EngineSettings, mp: pytest.MonkeyPatch) -> AgentEngine:
        pydantic_ai(vendor, settings, mp)
        return PydanticAIEngine(settings, store)

    return build


@asyncio_test
async def test_pydantic_ai_saves_the_text_in_its_session_and_none_of_the_structured_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = EverySave()

    events, _ = await round_of(pydantic_ai_on(store), draw, {}, monkeypatch)

    [result] = [event for event in events if isinstance(event, ToolResult)]
    assert result.structured_content == LAYOUT
    # Once with the tool round, before the model is asked again, and once with the answer.
    assert len(store.saved) == 2
    for history in store.saved:
        [kept] = [
            part
            for message in history
            if isinstance(message, ModelRequest)
            for part in message.parts
            if isinstance(part, ToolReturnPart)
        ]
        assert (kept.content, kept.metadata) == (DRAWN, None)
