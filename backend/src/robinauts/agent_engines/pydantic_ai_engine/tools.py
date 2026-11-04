# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The agent's tools: toolsets of its MCP servers with the server's credential, and one toolset of
its function tools."""

from __future__ import annotations

import base64
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from pydantic_ai import RunContext, Tool
from pydantic_ai.exceptions import ApprovalRequired, CallDeferred, ModelRetry, ToolFailed
from pydantic_ai.mcp import MCPToolset
from pydantic_ai.toolsets import AbstractToolset, FunctionToolset, ToolsetTool, WrapperToolset

from robinauts.agent_engines.contract.domain import (
    AgentDefinition,
    FunctionTool,
    FunctionToolError,
    ToolServerAuth,
    ToolServerConfig,
)
from robinauts.agent_engines.contract.ports import EngineSettings


def toolset_for(server: ToolServerConfig, settings: EngineSettings) -> AbstractToolset[Any]:
    headers = {}
    if server.auth is ToolServerAuth.BEARER:
        headers["Authorization"] = f"Bearer {settings.tool_secrets.secret_for(server.id)}"
    elif server.auth is ToolServerAuth.BASIC:
        pair = f"{server.user}:{settings.tool_secrets.secret_for(server.id)}"
        headers["Authorization"] = f"Basic {base64.b64encode(pair.encode()).decode('ascii')}"
    elif server.auth is ToolServerAuth.HEADER:
        headers[server.header] = settings.tool_secrets.secret_for(server.id)
    # Nothing connects here: the agent's run opens the session and closes it when the run ends.
    toolset: AbstractToolset[Any] = MCPToolset(
        server.url,
        headers=headers,
        init_timeout=server.timeout_seconds,
        read_timeout=server.timeout_seconds,
        tool_error_behavior="failed",
    )
    if server.exclude:
        excluded = set(server.exclude)
        toolset = toolset.filtered(lambda _context, tool: tool.name not in excluded)
    return FailuresToModel(toolset)


@dataclass
class FailuresToModel(WrapperToolset[Any]):
    """Every failed tool call is a failed result the model sees, and the turn goes on: a call
    that raised, such as one that timed out, and one the server answered with an error. Neither
    counts against the tool's retries, which would fail the turn the second time. A function
    tool's ``FunctionToolError`` is told in its own words, anything else by its kind alone."""

    async def call_tool(
        self, name: str, tool_args: dict[str, Any], ctx: RunContext[Any], tool: ToolsetTool[Any]
    ) -> Any:
        try:
            return await super().call_tool(name, tool_args, ctx, tool)
        except (ToolFailed, CallDeferred, ApprovalRequired):
            raise
        except ModelRetry as retry:
            raise ToolFailed(retry.message) from retry
        except FunctionToolError as error:
            raise ToolFailed(str(error)) from error
        except Exception as error:
            raise ToolFailed(f"The tool call failed: {type(error).__name__}.") from error


def toolsets_for(agent: AgentDefinition, settings: EngineSettings) -> list[AbstractToolset[Any]]:
    servers = settings.models.tool_servers
    return [toolset_for(servers[server_id], settings) for server_id in agent.tools]


def function_toolsets(functions: Iterable[FunctionTool]) -> list[AbstractToolset[Any]]:
    tools = [_function_tool(function) for function in functions]
    return [FailuresToModel(FunctionToolset(tools))] if tools else []


def _function_tool(function: FunctionTool) -> Tool[Any]:
    async def call(**arguments: Any) -> str:
        return await function.call(arguments)

    # A JSON Schema as it is: Pydantic AI hands the arguments over unchecked, as the tool expects.
    return Tool.from_schema(
        call,
        name=function.name,
        description=function.description,
        json_schema=dict(function.input_schema),
    )
