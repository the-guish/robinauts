# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The agent's tools, listed from its MCP servers with the server's credential."""

from __future__ import annotations

import base64
from datetime import timedelta

from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.sessions import StreamableHttpConnection

from robinauts.agent_engines.contract.domain import (
    AgentDefinition,
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
    timeout = timedelta(seconds=server.timeout_seconds)
    return {
        "transport": "streamable_http",
        "url": server.url,
        "headers": headers,
        "timeout": timeout,
        "sse_read_timeout": timeout,
    }


async def tools_for(agent: AgentDefinition, settings: EngineSettings) -> list[BaseTool]:
    if not agent.tools:
        return []
    servers = settings.models.tool_servers
    client = MultiServerMCPClient(
        {server_id: connection_for(servers[server_id], settings) for server_id in agent.tools}
    )
    # Listing opens a session per server and each tool opens its own per call: nothing to close.
    return await client.get_tools()
