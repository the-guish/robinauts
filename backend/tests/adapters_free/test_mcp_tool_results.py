# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""An MCP tool's result, on both engines, from a real MCP server: the model is sent its text,
and the engine reports its structured content apart (``docs/architecture/mcp-apps.md``).

The engines' own MCP clients make the call; only the model is a stand-in, which keeps what it
was sent (``chat_completions.Vendor``).
"""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import Callable
from typing import Any

import pytest
from mcp.types import CallToolResult, TextContent
from test_engines_over_chat_completions import SETTINGS
from test_tool_server_exclude import Build, langchain

from robinauts.agent_engines.contract.domain import (
    AgentDefinition,
    Event,
    ToolResult,
    ToolServerAuth,
    ToolServerConfig,
)
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


@pytest.mark.parametrize("build", [langchain], ids=["langchain"])
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
