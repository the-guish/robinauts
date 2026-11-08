# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The MCP apps of an agent's tool servers, for the interface's side panel
(``docs/adr/0007-mcp-apps-in-the-side-panel.md``).

An app is a tool whose ``_meta.ui.resourceUri`` names a ``ui://`` resource: one HTML document,
the view, which the panel frames. An agent's apps are those of the tool servers it names, less
the tools its server's ``exclude`` keeps from it. Web reads the view and relays the view's tool
calls; the view never reaches a server itself.

A server is reached with its configured credential, read from its ``secret_env`` the way the
worker reads it (``worker/engine_settings.py``), and within its ``timeout_seconds``. A server
that cannot be reached is logged by its id and the kind of failure, never by what it said.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from robinauts.controller.contract.domain import UnknownAgentError
from robinauts.shared_domain.domain import (
    AgentConfig,
    Config,
    MissingSecretError,
    ToolServerAuth,
    ToolServerConfig,
)
from robinauts.web.mcp_client import McpError, McpSession, session

SecretLookup = Callable[[str], str | None]

UI_SCHEME = "ui://"
UI_MIME_TYPE = "text/html;profile=mcp-app"
APP_VISIBILITY = "app"
MAX_TOOL_PAGES = 20
"""The most pages of ``tools/list`` read from one server."""

NO_SUCH_APP = "the agent has no app of that name"

FAILURES = (McpError, MissingSecretError, TimeoutError)
"""A server that cannot be reached, cannot be asked, or does not answer in time."""

log = logging.getLogger(__name__)


class NoSuchAppError(Exception):
    """An app the agent does not have, or that cannot be reached. One answer for all of them:
    which servers an agent names, and which of them is down, is not the asker's to learn."""

    def __init__(self) -> None:
        super().__init__(NO_SUCH_APP)


class UnansweredCallError(Exception):
    """A tool call the server did not answer with a result."""

    def __init__(self) -> None:
        super().__init__("the tool server did not answer the call")


@dataclass(frozen=True, slots=True)
class App:
    server: str
    tool: str
    title: str
    description: str

    @property
    def id(self) -> str:
        return f"{self.server}/{self.tool}"


@dataclass(frozen=True, slots=True)
class ToolResult:
    """An MCP ``CallToolResult``, as the server gave it."""

    content: list[Any]
    structured_content: dict[str, Any] | None
    is_error: bool


# --- what a server's answers say: pure ------------------------------------------------


def ui_of(tool: Mapping[str, Any]) -> Mapping[str, Any]:
    """The tool's ``_meta.ui``, empty when it has none."""
    meta = tool.get("_meta")
    ui = meta.get("ui") if isinstance(meta, Mapping) else None
    return ui if isinstance(ui, Mapping) else {}


def view_of(tool: Mapping[str, Any]) -> str | None:
    """The ``ui://`` resource of the tool's view, ``None`` for a tool without one."""
    uri = ui_of(tool).get("resourceUri")
    if isinstance(uri, str) and uri.startswith(UI_SCHEME) and len(uri) > len(UI_SCHEME):
        return uri
    return None


def callable_by_apps(tool: Mapping[str, Any]) -> bool:
    """Whether a view may call the tool: no ``visibility`` means model and app alike."""
    visibility = ui_of(tool).get("visibility")
    if visibility is None:
        return True
    return isinstance(visibility, list) and APP_VISIBILITY in visibility


def app_of(server: str, tool: Mapping[str, Any]) -> App | None:
    """The app the tool is: one with a view, that a view may call, under a name a path can
    carry; ``None`` for any other tool."""
    name = tool.get("name")
    if not isinstance(name, str) or not name or "/" in name or not name.isprintable():
        return None
    if view_of(tool) is None or not callable_by_apps(tool):
        return None
    title = tool.get("title")
    if not isinstance(title, str) or not title:
        annotations = tool.get("annotations")
        title = annotations.get("title") if isinstance(annotations, Mapping) else None
    description = tool.get("description")
    return App(
        server=server,
        tool=name,
        title=title if isinstance(title, str) and title else name,
        description=description if isinstance(description, str) else "",
    )


def is_view(mime_type: object) -> bool:
    return isinstance(mime_type, str) and mime_type.replace(" ", "").lower() == UI_MIME_TYPE


def view_text(result: Mapping[str, Any], uri: str) -> str | None:
    """The HTML of the view at ``uri`` in a ``resources/read`` result, ``None`` without one."""
    contents = result.get("contents")
    if not isinstance(contents, list):
        return None
    for item in contents:
        if not isinstance(item, Mapping) or item.get("uri") != uri:
            continue
        if not is_view(item.get("mimeType")):
            continue
        text = item.get("text")
        if isinstance(text, str):
            return text
        blob = item.get("blob")
        if isinstance(blob, str):
            try:
                return base64.b64decode(blob, validate=True).decode("utf-8")
            except (binascii.Error, UnicodeDecodeError):
                return None
    return None


def tool_result(result: Mapping[str, Any]) -> ToolResult | None:
    """The ``CallToolResult`` in a ``tools/call`` result, ``None`` when it is not one."""
    content = result.get("content")
    structured = result.get("structuredContent")
    is_error = result.get("isError", False)
    if not isinstance(content, list) or not isinstance(is_error, bool):
        return None
    if structured is not None and not isinstance(structured, dict):
        return None
    return ToolResult(content=content, structured_content=structured, is_error=is_error)


# --- the agent's servers ----------------------------------------------------------------


class Apps:
    """The apps of each configured agent, over the configured tool servers."""

    def __init__(self, config: Config, secret_for: SecretLookup) -> None:
        self._config = config
        self._secret_for = secret_for

    def _agent(self, agent_id: str) -> AgentConfig:
        agent = self._config.agents.get(agent_id)
        if agent is None:
            raise UnknownAgentError(agent_id)
        return agent

    def servers_of(self, agent_id: str) -> list[ToolServerConfig]:
        """The tool servers the agent names; its built-in tools are not servers."""
        servers = self._config.tool_servers
        return [servers[name] for name in self._agent(agent_id).tools if name in servers]

    def _server(self, agent_id: str, server_id: str) -> ToolServerConfig:
        server = next((s for s in self.servers_of(agent_id) if s.id == server_id), None)
        if server is None:
            raise NoSuchAppError()
        return server

    def _headers(self, server: ToolServerConfig) -> dict[str, str]:
        """The server's credential, as the engines send it."""
        if server.auth is ToolServerAuth.NONE:
            return {}
        secret = self._secret_for(server.secret_env) if server.secret_env else None
        if not secret:
            raise MissingSecretError(f"no secret for tool server {server.id!r}")
        if server.auth is ToolServerAuth.BEARER:
            return {"authorization": f"Bearer {secret}"}
        if server.auth is ToolServerAuth.BASIC:
            pair = base64.b64encode(f"{server.user}:{secret}".encode()).decode("ascii")
            return {"authorization": f"Basic {pair}"}
        return {server.header: secret}

    @asynccontextmanager
    async def _session(self, server: ToolServerConfig) -> AsyncIterator[McpSession]:
        """A session with the server, the whole of it within the server's timeout."""
        async with asyncio.timeout(server.timeout_seconds):
            headers = self._headers(server)
            async with session(server.url, headers, server.timeout_seconds) as opened:
                yield opened

    async def _tools(self, opened: McpSession, server: ToolServerConfig) -> list[Mapping[str, Any]]:
        """The server's tools that the agent is given: those its ``exclude`` does not name."""
        tools: list[Mapping[str, Any]] = []
        cursor: str | None = None
        for _ in range(MAX_TOOL_PAGES):
            page = await opened.request("tools/list", {"cursor": cursor} if cursor else None)
            listed = page.get("tools")
            if not isinstance(listed, list):
                raise McpError("the tool server's tools/list answer has no tools")
            tools += [t for t in listed if isinstance(t, Mapping)]
            cursor = page.get("nextCursor")
            if not isinstance(cursor, str) or not cursor:
                break
        return [t for t in tools if t.get("name") not in server.exclude]

    async def _tool(
        self, opened: McpSession, server: ToolServerConfig, name: str
    ) -> Mapping[str, Any] | None:
        return next((t for t in await self._tools(opened, server) if t.get("name") == name), None)

    @staticmethod
    def _failed(server: ToolServerConfig, doing: str, error: Exception) -> None:
        """Log a server that failed by its id and the kind of failure: our words alone."""
        reason = str(error) if isinstance(error, McpError) else type(error).__name__
        if isinstance(error, TimeoutError):
            reason = f"no answer within {server.timeout_seconds:g} seconds"
        log.warning("tool server %s failed %s: %s", server.id, doing, reason)

    async def _apps_of(self, server: ToolServerConfig) -> list[App]:
        try:
            async with self._session(server) as opened:
                tools = await self._tools(opened, server)
        except FAILURES as error:
            self._failed(server, "listing its apps, left out", error)
            return []
        return [app for app in (app_of(server.id, t) for t in tools) if app is not None]

    async def of(self, agent_id: str) -> list[App]:
        """The agent's apps, server by server in the order it names them. A server that cannot
        be reached is left out."""
        found = await asyncio.gather(*(self._apps_of(s) for s in self.servers_of(agent_id)))
        return [app for apps in found for app in apps]

    async def view(self, agent_id: str, server_id: str, tool: str) -> str:
        """The HTML of the tool's view; ``NoSuchAppError`` for a tool without one, and for a
        server that cannot be reached."""
        server = self._server(agent_id, server_id)
        try:
            async with self._session(server) as opened:
                uri = view_of(await self._tool(opened, server, tool) or {})
                if uri is None:
                    raise NoSuchAppError()
                text = view_text(await opened.request("resources/read", {"uri": uri}), uri)
        except FAILURES as error:
            self._failed(server, "reading a view", error)
            raise NoSuchAppError() from error
        if text is None:
            log.warning("tool server %s has no view for its tool's resource", server.id)
            raise NoSuchAppError()
        return text

    async def call(
        self, agent_id: str, server_id: str, tool: str, arguments: Mapping[str, Any]
    ) -> ToolResult:
        """The server's result for the call, as it gave it. ``NoSuchAppError`` for a tool the
        agent is not given or a view may not call; ``UnansweredCallError`` for a server that
        could not be reached or did not answer with a result."""
        server = self._server(agent_id, server_id)
        try:
            async with self._session(server) as opened:
                found = await self._tool(opened, server, tool)
                if found is None or not callable_by_apps(found):
                    raise NoSuchAppError()
                called = await opened.request("tools/call", {"name": tool, "arguments": arguments})
        except FAILURES as error:
            self._failed(server, "a call from a view", error)
            raise UnansweredCallError() from error
        result = tool_result(called)
        if result is None:
            log.warning("tool server %s answered a call with no CallToolResult", server.id)
            raise UnansweredCallError()
        return result
