# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""A real MCP server for the tests: FastMCP over streamable HTTP, on this machine's loopback.

Besides plain tools it offers MCP apps (``View``): a tool whose ``_meta.ui.resourceUri`` names
a ``ui://`` resource, the view's HTML. Given a ``token``, it answers 401 to any request that
does not carry it as ``Authorization: Bearer``.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import uvicorn
from mcp.server.fastmcp import FastMCP
from starlette.types import ASGIApp, Receive, Scope, Send

VIEW_MIME_TYPE = "text/html;profile=mcp-app"


@dataclass(frozen=True)
class View:
    """``tool``, offered as an app whose view is ``html``. ``visibility`` is the tool's
    ``_meta.ui.visibility``, left out when ``None``."""

    tool: Callable[..., Any]
    html: str
    visibility: tuple[str, ...] | None = None
    title: str | None = None

    @property
    def uri(self) -> str:
        return f"ui://tests/{self.tool.__name__}.html"

    @property
    def meta(self) -> dict[str, Any]:
        ui: dict[str, Any] = {"resourceUri": self.uri}
        if self.visibility is not None:
            ui["visibility"] = list(self.visibility)
        return {"ui": ui}


class BearerOnly:
    """401 to an HTTP request that does not carry the token."""

    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app = app
        self.expected = f"Bearer {token}".encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and dict(scope["headers"]).get(b"authorization") != (
            self.expected
        ):
            await send({"type": "http.response.start", "status": 401, "headers": []})
            await send({"type": "http.response.body", "body": b""})
            return
        await self.app(scope, receive, send)


def serve_view(view: View) -> Callable[[], str]:
    def read() -> str:
        return view.html

    return read


@contextmanager
def mcp_server(
    *tools: Callable[..., Any],
    views: Sequence[View] = (),
    token: str | None = None,
    sessions: bool = False,
    json_response: bool = False,
) -> Iterator[str]:
    """The server's URL while it offers ``tools`` and ``views``, stopped when the block ends.
    With ``sessions`` it names a session in ``Mcp-Session-Id``; with ``json_response`` it
    answers JSON rather than server-sent events."""
    server = FastMCP("tests", stateless_http=not sessions, json_response=json_response)
    for tool in tools:
        server.add_tool(tool)
    for view in views:
        server.add_tool(view.tool, title=view.title, meta=view.meta)
        server.resource(view.uri, mime_type=VIEW_MIME_TYPE)(serve_view(view))
    app: ASGIApp = server.streamable_http_app()
    if token is not None:
        app = BearerOnly(app, token)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    running = uvicorn.Server(config)
    thread = threading.Thread(target=running.run, daemon=True)
    thread.start()
    for _ in range(500):
        if running.started:
            break
        time.sleep(0.01)
    try:
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        running.should_exit = True
        thread.join(10)
