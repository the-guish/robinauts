# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The agent's tools: listed from its MCP servers with the server's credential, and its function
tools made LangChain tools.

An MCP tool may name a view, and say who may call it, in its ``_meta.ui``
(``docs/architecture/mcp-apps.md``). The adapter keeps a tool's ``_meta`` on the tool's metadata:
``for_the_model`` and ``has_view`` read it there."""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Iterable, Mapping
from datetime import timedelta
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.sessions import StreamableHttpConnection

from robinauts.agent_engines.contract.domain import (
    AgentDefinition,
    FunctionTool,
    ToolServerAuth,
    ToolServerConfig,
)
from robinauts.agent_engines.contract.ports import EngineSettings


def connection_for(server: ToolServerConfig, settings: EngineSettings) -> StreamableHttpConnection:
    headers = {}
    if server.auth is ToolServerAuth.BEARER:
        headers["Authorization"] = f"Bearer {settings.tool_secrets.secret_for(server.id)}"
    elif server.auth is ToolServerAuth.BASIC:
        pair = f"{server.user}:{settings.tool_secrets.secret_for(server.id)}"
        headers["Authorization"] = f"Basic {base64.b64encode(pair.encode()).decode('ascii')}"
    elif server.auth is ToolServerAuth.HEADER:
        headers[server.header] = settings.tool_secrets.secret_for(server.id)
    timeout = timedelta(seconds=server.timeout_seconds)
    return {
        "transport": "streamable_http",
        "url": server.url,
        "headers": headers,
        "timeout": timeout,
        "sse_read_timeout": timeout,
        # The session's own: past the transport's alone, a call is never answered and waits on.
        "session_kwargs": {"read_timeout_seconds": timeout},
    }


async def tools_for(agent: AgentDefinition, settings: EngineSettings) -> list[BaseTool]:
    if not agent.tools:
        return []
    servers = settings.models.tool_servers
    # One connection per server, however often the agent names it.
    connections = {s: connection_for(servers[s], settings) for s in agent.tools}
    client = MultiServerMCPClient(connections)
    # Listing opens a session per server and each tool opens its own per call: nothing to close.
    # Per server, so that each one's `exclude` applies to its own tools' names.
    listed = await asyncio.gather(*(client.get_tools(server_name=s) for s in connections))
    return [
        tool
        for server_id, tools in zip(connections, listed, strict=True)
        for tool in tools
        if tool.name not in servers[server_id].exclude and for_the_model(tool)
    ]


def ui_of(tool: object) -> Mapping[str, Any]:
    """An MCP tool's ``_meta.ui``, empty when it has none, as for a tool that is no MCP
    server's."""
    metadata = getattr(tool, "metadata", None)
    meta = metadata.get("_meta") if isinstance(metadata, Mapping) else None
    ui = meta.get("ui") if isinstance(meta, Mapping) else None
    return ui if isinstance(ui, Mapping) else {}


def for_the_model(tool: object) -> bool:
    """Whether the model may call the tool: its ``visibility`` names ``model``, or it has none.
    A tool for ``app`` alone is one only a view calls."""
    visibility = ui_of(tool).get("visibility")
    return not isinstance(visibility, list) or "model" in visibility


def has_view(tool: object) -> bool:
    """Whether the tool names a view, in ``resourceUri``."""
    return isinstance(ui_of(tool).get("resourceUri"), str)


def function_tools(functions: Iterable[FunctionTool]) -> list[BaseTool]:
    return [_function_tool(function) for function in functions]


def _function_tool(function: FunctionTool) -> BaseTool:
    async def call(**arguments: Any) -> str:
        return await function.call(arguments)

    # A JSON Schema as it is: LangChain hands the arguments over unchecked, as the tool expects.
    return StructuredTool.from_function(
        coroutine=call,
        name=function.name,
        description=function.description,
        args_schema=dict(function.input_schema),
    )
