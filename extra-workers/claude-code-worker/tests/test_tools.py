# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from robinauts.agent_engines.contract.domain import (
    AgentDefinition,
    FunctionTool,
    FunctionToolError,
    ModelsConfig,
    ToolServerAuth,
    ToolServerConfig,
)
from robinauts.agent_engines.contract.ports import ToolSecretLookup
from robinauts_claude_worker import tools

MODELS = ModelsConfig(
    tool_servers={
        "gh": ToolServerConfig("gh", "https://gh/mcp", "GH_TOKEN"),
        "wiki": ToolServerConfig("wiki", "https://wiki/mcp", "WIKI", ToolServerAuth.BASIC, "bot"),
        "composio": ToolServerConfig(
            "composio",
            "https://c/mcp",
            "C_KEY",
            ToolServerAuth.HEADER,
            header="x-api-key",
            exclude=("COMPOSIO_MANAGE_SKILL", "OTHER"),
        ),
        "learn": ToolServerConfig("learn", "https://learn/mcp", auth=ToolServerAuth.NONE),
    }
)


class Secrets(ToolSecretLookup):
    def secret_for(self, server_id: str) -> str:
        return f"secret-of-{server_id}"


async def search(arguments: Mapping[str, Any]) -> str:
    if arguments.get("query") == "boom":
        raise FunctionToolError("nothing matches boom")
    if arguments.get("query") == "crash":
        raise RuntimeError("a bug, with details the model must not see")
    return f"found {arguments['query']}"


SEARCH = FunctionTool(
    "search_sessions",
    "Search your conversations",
    {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
    search,
)


def test_the_tool_servers_as_mcp_servers_with_their_credentials() -> None:
    agent = AgentDefinition("", tools=("gh", "wiki", "composio", "learn"))
    servers = tools.mcp_servers(agent, MODELS, Secrets())
    assert servers == {
        "gh": {
            "type": "http",
            "url": "https://gh/mcp",
            "headers": {"Authorization": "Bearer secret-of-gh"},
        },
        "wiki": {
            "type": "http",
            "url": "https://wiki/mcp",
            "headers": {"Authorization": "Basic Ym90OnNlY3JldC1vZi13aWtp"},
        },
        "composio": {
            "type": "http",
            "url": "https://c/mcp",
            "headers": {"x-api-key": "secret-of-composio"},
        },
        "learn": {"type": "http", "url": "https://learn/mcp", "headers": {}},
    }
    assert tools.excluded(agent, MODELS) == [
        "mcp__composio__COMPOSIO_MANAGE_SKILL",
        "mcp__composio__OTHER",
    ]
    assert tools.allowed(agent) == ["mcp__gh", "mcp__wiki", "mcp__composio", "mcp__learn"]


def test_the_functions_run_here_as_one_in_process_server() -> None:
    agent = AgentDefinition("", functions=(SEARCH,))
    assert tools.sdk_server(AgentDefinition("")) is None
    server = tools.sdk_server(agent)
    assert server is not None
    assert (server["type"], server["name"]) == ("sdk", "robinauts")
    assert tools.allowed(agent) == ["mcp__robinauts__search_sessions"]
    wrapped = tools.wrap(SEARCH)
    assert (wrapped.name, wrapped.description) == ("search_sessions", "Search your conversations")
    assert asyncio.run(wrapped.handler({"query": "cats"})) == {
        "content": [{"type": "text", "text": "found cats"}]
    }
    assert asyncio.run(wrapped.handler({"query": "boom"})) == {
        "content": [{"type": "text", "text": "nothing matches boom"}],
        "is_error": True,
    }
    assert asyncio.run(wrapped.handler({"query": "crash"})) == {
        "content": [{"type": "text", "text": "RuntimeError"}],
        "is_error": True,
    }
