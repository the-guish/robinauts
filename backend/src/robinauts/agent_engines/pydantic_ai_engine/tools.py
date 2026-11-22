# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The agent's tools: toolsets of its MCP servers with the server's credential, and one toolset of
its function tools.

An MCP result has text for the model and may have structured content for a view
(``docs/architecture/mcp-apps.md``). Pydantic AI hands the model the structured content in the
text's place when a result has both. ``TextToModel`` puts the text back and keeps the structured
content on the tool return's metadata, where the engine reads it."""

from __future__ import annotations

import base64
from collections.abc import Awaitable, Callable, Iterable
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from mcp.types import TextContent
from pydantic_ai import RunContext, Tool, ToolReturn
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
    heard_from(toolset.client)
    toolset = TextToModel(toolset)
    if server.exclude:
        excluded = set(server.exclude)
        toolset = toolset.filtered(lambda _context, tool: tool.name not in excluded)
    return FailuresToModel(toolset)


STRUCTURED_CONTENT = "structured_content"
"""The key of a tool return's metadata that holds an MCP result's structured content."""

_HEARD: ContextVar[list[Any] | None] = ContextVar("robinauts_mcp_results", default=None)
"""The MCP results of the tool call this task is making, as the client received them."""


def heard_from(client: Any) -> None:
    """Have the toolset's MCP client hand each result it receives to the tool call that asked,
    as the server gave it: the toolset itself returns a result already mapped for the model."""
    call: Callable[..., Awaitable[Any]] = client.call_tool

    async def call_tool(*args: Any, **kwargs: Any) -> Any:
        result = await call(*args, **kwargs)
        heard = _HEARD.get()
        if heard is not None:
            heard.append(result)
        return result

    client.call_tool = call_tool


@dataclass
class TextToModel(WrapperToolset[Any]):
    """An MCP result with structured content: its text is what the model reads, and the
    structured content is kept on the return's metadata, apart. A result of text parts alone is
    their text, one string, or a list of several; any other result is as Pydantic AI maps it."""

    async def call_tool(
        self, name: str, tool_args: dict[str, Any], ctx: RunContext[Any], tool: ToolsetTool[Any]
    ) -> Any:
        heard: list[Any] = []
        token = _HEARD.set(heard)
        try:
            mapped = await super().call_tool(name, tool_args, ctx, tool)
        finally:
            _HEARD.reset(token)
        result = heard[-1] if heard else None
        structured = getattr(result, "structured_content", None)
        if not isinstance(structured, dict):
            return mapped
        parts = list(getattr(result, "content", None) or [])
        texts = [part.text for part in parts if isinstance(part, TextContent)]
        if texts and len(texts) == len(parts):
            mapped = texts[0] if len(texts) == 1 else texts
        return ToolReturn(return_value=mapped, metadata={STRUCTURED_CONTENT: structured})


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
