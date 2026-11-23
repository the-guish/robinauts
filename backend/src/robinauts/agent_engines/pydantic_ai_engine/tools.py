# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The agent's tools: toolsets of its MCP servers with the server's credential, and one toolset of
its function tools.

A tool with a view answers text for the model and may answer structured content for the view
(``docs/architecture/mcp-apps.md``). Pydantic AI's toolset keeps that answer on the tool's return,
as the server gave it, and hands the model the structured content in the text's place when the
answer has both. ``TextToModel`` puts the text back and keeps the structured content on the
return's metadata, where the engine reads it. The answer of any other tool is as Pydantic AI
maps it."""

from __future__ import annotations

import base64
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

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
    toolset: AbstractToolset[Any] = TextToModel(
        MCPToolset(
            server.url,
            headers=headers,
            init_timeout=server.timeout_seconds,
            read_timeout=server.timeout_seconds,
            tool_error_behavior="failed",
        )
    )
    if server.exclude:
        excluded = set(server.exclude)
        toolset = toolset.filtered(lambda _context, tool: tool.name not in excluded)
    return FailuresToModel(toolset)


STRUCTURED_CONTENT = "structured_content"
"""The key of a tool return's metadata that holds an MCP result's structured content."""

KEPT_FOR_A_VIEW = "mcp_app"
"""The key of a tool return's metadata under which Pydantic AI's toolset keeps what a view needs
of a call to a tool with one (``pydantic_ai.mcp.MCPAppToolResult``). Its ``result`` is the
server's answer in MCP's wire form."""


def answer_of(metadata: object) -> Mapping[str, Any] | None:
    """The server's answer to a call of a tool with a view, in MCP's wire form, from the metadata
    of the tool's return. ``None`` for the metadata of any other return."""
    kept = metadata.get(KEPT_FOR_A_VIEW) if isinstance(metadata, Mapping) else None
    answer = kept.get("result") if isinstance(kept, Mapping) else None
    return answer if isinstance(answer, Mapping) else None


@dataclass
class TextToModel(WrapperToolset[Any]):
    """The answer of a tool with a view: its text is what the model reads, and its structured
    content is kept on the return's metadata, apart. An answer of text blocks alone is their
    text, one string, or a list of several; any other is as Pydantic AI maps it. The rest of what
    Pydantic AI keeps for a view goes no further: the session holds what the model read."""

    async def call_tool(
        self, name: str, tool_args: dict[str, Any], ctx: RunContext[Any], tool: ToolsetTool[Any]
    ) -> Any:
        returned = await super().call_tool(name, tool_args, ctx, tool)
        answer = answer_of(returned.metadata) if isinstance(returned, ToolReturn) else None
        if answer is None:
            return returned
        mapped = returned.return_value
        structured = answer.get("structuredContent")
        if not isinstance(structured, dict):
            return mapped
        blocks = answer.get("content") or []
        texts = [block["text"] for block in blocks if block.get("type") == "text"]
        if texts and len(texts) == len(blocks):
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
