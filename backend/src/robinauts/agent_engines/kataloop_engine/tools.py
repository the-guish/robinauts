# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The agent's tool servers as kataloop takes them, each with its credential, and the toolbox
that reaches them.

The toolbox is kataloop's ``Toolbox`` port over the MCP SDK's streamable HTTP client, held to
kataloop's ``docs/specs/tools.md``: a session of its own per listing and per call, closed before
it returns; the server's headers on every request and its timeout on every wait; a result's
content one part per line, its structured content neither shown nor checked; and every timeout
reported alike. It stands in for kataloop's own
``McpToolbox``, which is built on ``mcp`` 2, until this build can lock that
(``backend/pyproject.toml``, ``docs/adr/0006-kataloop-engine.md``).
"""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any

import httpx
from kataloop_core import Toolbox, ToolOutput, ToolServer, ToolSpec
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import McpError
from mcp.types import CallToolResult, PaginatedRequestParams, TextContent

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
    return named, McpToolbox()


HttpClientFactory = Callable[[dict[str, str], httpx.Timeout], httpx.AsyncClient]
"""Makes the HTTP client of one MCP session from the server's headers and timeout, as
kataloop's own toolbox takes it. The toolbox closes the client when the session ends."""


def _http_client(headers: dict[str, str], timeout: httpx.Timeout) -> httpx.AsyncClient:
    return httpx.AsyncClient(headers=headers, timeout=timeout)


class McpToolbox(Toolbox):
    """The tools of MCP servers, reached over streamable HTTP. Nothing stays open between a
    listing or a call and the next, so the toolbox needs no closing and a cancelled turn leaves
    no session behind.

    ``http_client_factory`` makes the HTTP client of each session, from the server's headers and
    timeout, which the client must send and keep: by default an ``httpx.AsyncClient`` with them
    and nothing else. A test watches the requests through it.
    """

    def __init__(self, *, http_client_factory: HttpClientFactory = _http_client) -> None:
        self._http_client_factory = http_client_factory

    async def tools(self, server: ToolServer) -> Sequence[ToolSpec]:
        """Every page of the server's listing, in its order. What fails is raised: a listing
        is no tool call, and the turn fails with it."""
        try:
            async with self._session(server) as session:
                page = await session.list_tools()
                listed = list(page.tools)
                while page.nextCursor is not None:
                    following = PaginatedRequestParams(cursor=page.nextCursor)
                    page = await session.list_tools(params=following)
                    listed.extend(page.tools)
        except Exception as error:
            raise _failure(error, server) from None
        return [ToolSpec(tool.name, tool.description or "", tool.inputSchema) for tool in listed]

    async def call(self, server: ToolServer, name: str, arguments: Mapping[str, Any]) -> ToolOutput:
        """What the tool answered, and whether it reports an error. What fails is raised, and
        kataloop tells the model: a timeout as one, anything else by its type."""
        try:
            async with self._session(server) as session:
                result = await session.call_tool(name, dict(arguments))
        except Exception as error:
            raise _failure(error, server) from None
        return ToolOutput(_text(result), is_error=result.isError)

    @asynccontextmanager
    async def _session(self, server: ToolServer) -> AsyncIterator[ClientSession]:
        """An initialised session, closed with its HTTP client however the block ends."""
        http = self._http_client_factory(dict(server.headers), httpx.Timeout(server.timeout))
        waits = timedelta(seconds=server.timeout)
        async with (
            http,
            streamable_http_client(server.url, http_client=http) as (read, write, _),
            _Session(read, write, read_timeout_seconds=waits) as session,
        ):
            await session.initialize()
            yield session


class _Session(ClientSession):
    """The SDK's session, without its check of a result's structured content, as kataloop's own
    toolbox has it. The model is given a result's content and never its structured content, so
    the check protects nothing; and in a session that has not listed the tools, which is every
    call's, the SDK would list them again before each result to make it."""

    async def _validate_tool_result(self, name: str, result: CallToolResult) -> None:
        return None


def _text(result: CallToolResult) -> str:
    """What the model sees of a result: a text part as its text, any other as its JSON, compact
    and with its keys sorted, one part per line."""
    return "\n".join(
        (
            part.text
            if isinstance(part, TextContent)
            else json.dumps(
                part.model_dump(by_alias=True, mode="json", exclude_none=True),
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        )
        for part in result.content
    )


def _failure(error: Exception, server: ToolServer) -> Exception:
    """The error itself, out of the exception groups the SDK's task groups raise it in. A
    timeout of the SDK's or of the HTTP client's is a ``TimeoutError``, which kataloop reports
    as the timeout of its own: ``Timed out after N seconds.``"""
    leaves = _leaves(error)
    if any(_timed_out(leaf) for leaf in leaves):
        return TimeoutError(
            f"The MCP server {server.id!r} did not answer within {server.timeout:g} seconds"
        )
    for leaf in leaves:
        if isinstance(leaf, McpError):
            return leaf
    return leaves[0]


def _leaves(error: Exception) -> list[Exception]:
    if isinstance(error, ExceptionGroup):
        return [leaf for inner in error.exceptions for leaf in _leaves(inner)]
    return [error]


def _timed_out(error: Exception) -> bool:
    if isinstance(error, McpError):
        return error.error.code == httpx.codes.REQUEST_TIMEOUT
    return isinstance(error, httpx.TimeoutException | TimeoutError)
