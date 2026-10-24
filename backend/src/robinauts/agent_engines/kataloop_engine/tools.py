# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The agent's tool servers as kataloop takes them, each with its credential, and the toolbox
that reaches them.

The toolbox is kataloop's ``McpToolbox``, on the ``mcp`` 1 SDK that ``kataloop[mcp-1]`` holds it
to, beside the LangChain engine's MCP client (``backend/pyproject.toml``,
``docs/adr/0006-kataloop-engine.md``): a session of its own per listing and per call, the
server's headers on every request and its timeout on every wait, and every timeout reported
alike (kataloop's ``docs/specs/tools.md``).
"""

from __future__ import annotations

import base64

from kataloop import McpHttpClient, McpHttpTimeout, McpToolbox, Toolbox, ToolServer

from robinauts.agent_engines.contract.domain import (
    AgentDefinition,
    ToolServerAuth,
    ToolServerConfig,
)
from robinauts.agent_engines.contract.ports import EngineSettings


def headers_for(server: ToolServerConfig, settings: EngineSettings) -> dict[str, str]:
    """The credential the server is reached with; its secret is asked for only if it has one."""
    if server.auth is ToolServerAuth.BEARER:
        return {"Authorization": f"Bearer {settings.tool_secrets.secret_for(server.id)}"}
    if server.auth is ToolServerAuth.BASIC:
        pair = f"{server.user}:{settings.tool_secrets.secret_for(server.id)}"
        return {"Authorization": f"Basic {base64.b64encode(pair.encode()).decode('ascii')}"}
    if server.auth is ToolServerAuth.HEADER:
        return {server.header: settings.tool_secrets.secret_for(server.id)}
    return {}


def tool_server(server: ToolServerConfig, settings: EngineSettings) -> ToolServer:
    return ToolServer(
        id=server.id,
        url=server.url,
        headers=headers_for(server, settings),
        timeout=server.timeout_seconds,
        exclude=server.exclude,
    )


def tools_for(
    agent: AgentDefinition, settings: EngineSettings
) -> tuple[tuple[ToolServer, ...], Toolbox]:
    """The servers the agent names, once each and in its order, and the toolbox over them."""
    servers = settings.models.tool_servers
    named = tuple(tool_server(servers[s], settings) for s in dict.fromkeys(agent.tools))
    return named, McpToolbox(http_client_factory=http_client)


def http_client(headers: dict[str, str], timeout: McpHttpTimeout) -> McpHttpClient:
    """The HTTP client of one session with a tool server: the server's headers and its timeout,
    and nothing else, so that a proxy is ``HTTPS_PROXY``, as for the vendors, and a redirect is
    not followed. The class is the one the MCP SDK installed sends with, httpx's on ``mcp`` 1,
    under kataloop's name for it, so that this is the same on ``mcp`` 2. The toolbox closes the
    client with the session."""
    return McpHttpClient(headers=headers, timeout=timeout)
