# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The agent's tools, as Claude Code takes them: the configured tool servers as MCP servers over
HTTP with the credential each table's ``auth`` implies, the tools run in this process as one
in-process MCP server of the SDK's, and the names that let them run without asking."""

from __future__ import annotations

import base64
from collections.abc import Mapping
from typing import Any

from claude_agent_sdk import McpServerConfig, SdkMcpTool, create_sdk_mcp_server, tool

from robinauts.agent_engines.contract.domain import (
    AgentDefinition,
    FunctionTool,
    FunctionToolError,
    ModelsConfig,
    ToolServerAuth,
)
from robinauts.agent_engines.contract.ports import ToolSecretLookup

SDK_SERVER = "robinauts"
"""The name of the in-process server that holds the tools run here, such as the search of a
person's conversations: ``mcp__robinauts__<tool>`` to Claude Code."""


def mcp_servers(
    agent: AgentDefinition, models: ModelsConfig, secrets: ToolSecretLookup
) -> dict[str, McpServerConfig]:
    """The agent's tool servers, by id, each over HTTP with its header."""
    servers: dict[str, McpServerConfig] = {}
    for server_id in agent.tools:
        table = models.tool_servers[server_id]
        headers: dict[str, str] = {}
        if table.auth is ToolServerAuth.BEARER:
            headers["Authorization"] = f"Bearer {secrets.secret_for(server_id)}"
        elif table.auth is ToolServerAuth.BASIC:
            pair = f"{table.user}:{secrets.secret_for(server_id)}".encode()
            headers["Authorization"] = "Basic " + base64.b64encode(pair).decode()
        elif table.auth is ToolServerAuth.HEADER:
            headers[table.header] = secrets.secret_for(server_id)
        servers[server_id] = {"type": "http", "url": table.url, "headers": headers}
    return servers


def excluded(agent: AgentDefinition, models: ModelsConfig) -> list[str]:
    """The tools of the agent's servers that a table's ``exclude`` leaves out, as Claude Code
    names them."""
    return [
        f"mcp__{server_id}__{name}"
        for server_id in agent.tools
        for name in models.tool_servers[server_id].exclude
    ]


def allowed(agent: AgentDefinition) -> list[str]:
    """The agent's tool servers, whole, and each tool run here: what runs without asking."""
    servers = [f"mcp__{server_id}" for server_id in agent.tools]
    functions = [f"mcp__{SDK_SERVER}__{function.name}" for function in agent.functions]
    return servers + functions


def wrap(function: FunctionTool) -> SdkMcpTool[Any]:
    """A tool run in this process, as the SDK's in-process server runs one: a failure the tool
    means for the model is a result marked as an error, anything else its kind alone."""

    @tool(function.name, function.description, dict(function.input_schema))
    async def call(arguments: Mapping[str, Any]) -> dict[str, Any]:
        try:
            text = await function.call(dict(arguments))
        except FunctionToolError as failed:
            return {"content": [{"type": "text", "text": str(failed)}], "is_error": True}
        except Exception as error:
            return {"content": [{"type": "text", "text": type(error).__name__}], "is_error": True}
        return {"content": [{"type": "text", "text": text}]}

    return call


def sdk_server(agent: AgentDefinition) -> McpServerConfig | None:
    """The in-process server of the agent's functions, or ``None`` for an agent with none."""
    if not agent.functions:
        return None
    return create_sdk_mcp_server(SDK_SERVER, tools=[wrap(f) for f in agent.functions])
