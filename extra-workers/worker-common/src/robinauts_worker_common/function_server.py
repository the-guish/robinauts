# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The tools run in a worker's process, such as the search of a person's conversations, served
as an MCP server on loopback, for an agent's CLI that has no other way to call back: OpenCode's
server, say, which reaches tools over MCP alone.

The tools are bound to a turn (``AgentDefinition.functions``), so each turn's are served under a
token of their own, which the CLI sends as a bearer token: ``bind`` gives it, ``unbind`` takes
it back when the turn ends. A request with any other token is refused, so no other process on
the machine calls a turn's tools.

It speaks MCP's streamable HTTP transport in its plainest form: every message is a POST to
``/mcp``, every answer one JSON body, no event stream and no session. That is what an MCP client
must accept from a server. It is written on asyncio's streams alone: the worker's image carries
no web server.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import secrets
from collections.abc import Mapping, Sequence
from typing import Any

from robinauts.agent_engines.contract.domain import FunctionTool, FunctionToolError

_log = logging.getLogger(__name__)

PATH = "/mcp"
PROTOCOL_VERSION = "2025-06-18"
"""The version answered when the client asks for none this server knows."""
VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18")
SERVER_INFO = {"name": "robinauts", "version": "0.1.0"}
MAX_BODY = 16 * 1024 * 1024
HEADER_LIMIT = 64 * 1024


class FunctionServer:
    """Started by ``start`` on a port the operating system picks, stopped by ``stop``."""

    def __init__(self, host: str = "127.0.0.1") -> None:
        self._host = host
        self._server: asyncio.Server | None = None
        self._bound: dict[str, tuple[FunctionTool, ...]] = {}

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._connection, self._host, 0, limit=HEADER_LIMIT
        )

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None

    @property
    def url(self) -> str:
        assert self._server is not None, "the function server is not started"
        port = self._server.sockets[0].getsockname()[1]
        return f"http://{self._host}:{port}{PATH}"

    def bind(self, functions: Sequence[FunctionTool]) -> str:
        """A token under which these tools are served until ``unbind``."""
        token = secrets.token_urlsafe(32)
        self._bound[token] = tuple(functions)
        return token

    def unbind(self, token: str) -> None:
        self._bound.pop(token, None)

    async def _connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                request = await _read_request(reader)
                if request is None:
                    break
                method, path, headers, body = request
                status, answer = await self._answer(method, path, headers, body)
                await _write_response(writer, status, answer)
                if headers.get("connection", "").lower() == "close":
                    break
        except (ConnectionError, asyncio.IncompleteReadError, ValueError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _answer(
        self, method: str, path: str, headers: Mapping[str, str], body: bytes
    ) -> tuple[int, Any]:
        if path.split("?", 1)[0] != PATH:
            return 404, None
        scheme, _, token = headers.get("authorization", "").partition(" ")
        functions = self._bound.get(token) if scheme.lower() == "bearer" else None
        if functions is None:
            return 401, None
        if method == "DELETE":
            return 200, None
        if method != "POST":
            # No event stream: the client sends, this server answers in the same response.
            return 405, None
        try:
            message = json.loads(body)
        except ValueError:
            return 400, _error(None, -32700, "not JSON")
        if not isinstance(message, Mapping):
            return 400, _error(None, -32600, "one JSON-RPC message per request")
        if "id" not in message:
            return 202, None  # a notification, or a response to nothing this server asked
        return 200, await handle(functions, message)


async def handle(functions: Sequence[FunctionTool], message: Mapping[str, Any]) -> dict[str, Any]:
    """The answer to one JSON-RPC request of MCP's, for these tools."""
    id_ = message.get("id")
    method = message.get("method")
    params = message.get("params") or {}
    if method == "initialize":
        asked = params.get("protocolVersion")
        return _result(
            id_,
            {
                "protocolVersion": asked if asked in VERSIONS else PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": SERVER_INFO,
            },
        )
    if method == "ping":
        return _result(id_, {})
    if method == "tools/list":
        listed = [
            {
                "name": function.name,
                "description": function.description,
                "inputSchema": dict(function.input_schema),
            }
            for function in functions
        ]
        return _result(id_, {"tools": listed})
    if method == "tools/call":
        return _result(id_, await call(functions, params))
    return _error(id_, -32601, f"no method {method!r}")


async def call(functions: Sequence[FunctionTool], params: Mapping[str, Any]) -> dict[str, Any]:
    """A ``tools/call`` result: the tool's output, or a failure the model reads. A failure the
    tool means for the model is its message; anything else its kind alone."""
    name = params.get("name")
    function = next((f for f in functions if f.name == name), None)
    if function is None:
        return _text(f"no tool {name!r} in this turn", failed=True)
    arguments = params.get("arguments")
    try:
        text = await function.call(dict(arguments) if isinstance(arguments, Mapping) else {})
    except FunctionToolError as failed:
        return _text(str(failed), failed=True)
    except Exception as error:
        _log.warning("tool %s failed: %s", name, type(error).__name__)
        return _text(type(error).__name__, failed=True)
    return _text(text, failed=False)


def _text(text: str, *, failed: bool) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": failed}


def _result(id_: Any, result: Mapping[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def _error(id_: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


async def _read_request(
    reader: asyncio.StreamReader,
) -> tuple[str, str, dict[str, str], bytes] | None:
    """One HTTP/1.1 request, or ``None`` when the client closed the connection."""
    line = await reader.readline()
    if not line:
        return None
    parts = line.decode("latin-1").split()
    if len(parts) != 3:
        raise ValueError("not an HTTP request line")
    method, path, _ = parts
    headers: dict[str, str] = {}
    while True:
        header = await reader.readline()
        if header in (b"\r\n", b"\n", b""):
            break
        name, _, value = header.decode("latin-1").partition(":")
        headers[name.strip().lower()] = value.strip()
    length = int(headers.get("content-length") or 0)
    if length > MAX_BODY:
        raise ValueError("body too long")
    body = await reader.readexactly(length) if length else b""
    return method, path, headers, body


REASONS = {
    200: "OK",
    202: "Accepted",
    400: "Bad Request",
    401: "Unauthorized",
    404: "Not Found",
    405: "Method Not Allowed",
}


async def _write_response(writer: asyncio.StreamWriter, status: int, answer: Any) -> None:
    body = b"" if answer is None else json.dumps(answer).encode()
    head = [f"HTTP/1.1 {status} {REASONS.get(status, 'Error')}", f"Content-Length: {len(body)}"]
    if body:
        head.append("Content-Type: application/json")
    if status == 405:
        head.append("Allow: POST, DELETE")
    writer.write(("\r\n".join(head) + "\r\n\r\n").encode("latin-1") + body)
    await writer.drain()
