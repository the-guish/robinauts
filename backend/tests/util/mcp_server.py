# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""A real MCP server for the tests: FastMCP over streamable HTTP, on this machine's loopback.

Besides plain tools it offers MCP apps (``View``): a tool whose ``_meta.ui.resourceUri`` names
a ``ui://`` resource, the view's HTML. Given a ``token``, it answers 401 to any request that
does not carry it as ``Authorization: Bearer``.

``example_server`` runs one of the servers under ``mcp/`` as its own command, as an operator
would run it.
"""

from __future__ import annotations

import os
import socket
import subprocess
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
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
        named = server.resource(
            view.uri, name=view.tool.__name__, title=view.title, mime_type=VIEW_MIME_TYPE
        )
        named(serve_view(view))
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


EXAMPLES = Path(__file__).resolve().parents[3] / "mcp"
# A first run of the command installs the server's own locked environment.
START_SECONDS = 180


@contextmanager
def example_server(name: str, command: str, *arguments: str) -> Iterator[str]:
    """The URL of the server of ``mcp/<name>``, run as ``command`` with ``arguments`` on a port
    of its own, stopped when the block ends."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    # The backend's environment is not the server's: uv finds the server's own from its folder.
    environment = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
    line = [
        *("uv", "run", "--locked", "--directory", str(EXAMPLES / name)),
        *(command, "--port", str(port), *arguments),
    ]
    with subprocess.Popen(line, env=environment) as running:
        try:
            deadline = time.monotonic() + START_SECONDS
            while True:
                if running.poll() is not None:
                    raise RuntimeError(f"{command} exited with {running.returncode}")
                try:
                    socket.create_connection(("127.0.0.1", port), timeout=1).close()
                    break
                except OSError:
                    if time.monotonic() > deadline:
                        raise RuntimeError(f"{command} did not start") from None
                    time.sleep(0.2)
            yield f"http://127.0.0.1:{port}/mcp"
        finally:
            running.terminate()
            running.wait(timeout=10)
