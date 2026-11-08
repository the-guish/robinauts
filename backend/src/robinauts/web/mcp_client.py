# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Just enough of an MCP client, over streamable HTTP, for the side panel's apps.

The engines reach the tool servers through their frameworks' own clients, and ``mcp`` is not
imported outside them (``docs/architecture/rules.md``). Web asks a server three things of its
own (``web/apps.py``), so it speaks the protocol itself over httpx: ``initialize``, then
``notifications/initialized``, then the requests, in a session that ends when the caller is
done. Each request is a JSON-RPC POST; its answer is JSON, or a stream of server-sent events
that carries it. A redirect is not followed, so the credential goes to the configured URL alone.

Nothing here logs, and an ``McpError`` says what went wrong in words of ours alone: a server's
own error text can quote what it was sent, the credential included.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any

import httpx

PROTOCOL_VERSION = "2025-06-18"
"""What ``initialize`` asks for; the server's answer names the version the session speaks."""

MAX_ANSWER_BYTES = 8 * 1024 * 1024
"""The most of one answer read: a view is one HTML document, and a tool result is JSON."""

END_SECONDS = 5.0
"""How long the end of a session is waited for: past the caller's bound, when it ran out."""

ACCEPT = "application/json, text/event-stream"
JSON = "application/json"
EVENT_STREAM = "text/event-stream"

_LINE_END = re.compile(r"\r\n|\r|\n")


class McpError(Exception):
    """A server that could not be reached, or that answered with an error or not at all."""


class EventReader:
    """Server-sent events, fed as they arrive: the data of each event once it is complete.

    An event is complete at the blank line that ends it; one the stream cuts off is dropped,
    as the specification says. A ``\\r`` at the end of a piece is held back, in case the
    ``\\n`` of the same line ending comes with the next.
    """

    __slots__ = ("_data", "_pending")

    def __init__(self) -> None:
        self._pending = ""
        self._data: list[str] = []

    def feed(self, text: str) -> list[str]:
        buffer = self._pending + text
        held = ""
        if buffer.endswith("\r"):
            buffer, held = buffer[:-1], "\r"
        *lines, rest = _LINE_END.split(buffer)
        self._pending = rest + held
        complete: list[str] = []
        for line in lines:
            if not line:
                if self._data:
                    complete.append("\n".join(self._data))
                    self._data = []
                continue
            name, colon, value = line.partition(":")
            if name == "data":
                self._data.append(value.removeprefix(" ") if colon else "")
        return complete

    def close(self) -> list[str]:
        """The stream has ended, so a ``\\r`` held back ended its line after all."""
        if not self._pending.endswith("\r"):
            return []
        pending, self._pending = self._pending, ""
        return self.feed(pending[:-1] + "\n")


def answer_in(message: object, request_id: int) -> dict[str, Any] | None:
    """The result of request ``request_id`` when ``message`` is its answer, else ``None``;
    ``McpError`` for an answer that is an error or no result."""
    if isinstance(message, list):
        for item in message:
            found = answer_in(item, request_id)
            if found is not None:
                return found
        return None
    if not isinstance(message, dict) or message.get("id") != request_id:
        return None
    if "error" in message:
        raise McpError("the tool server answered with an error")
    if "result" not in message:
        return None
    result = message["result"]
    if not isinstance(result, dict):
        raise McpError("the tool server's answer is not a JSON-RPC result")
    return result


def parsed(text: str) -> object:
    try:
        return json.loads(text)
    except ValueError as error:
        raise McpError("the tool server's answer is not JSON") from error


def unreached(error: httpx.HTTPError) -> McpError:
    return McpError(f"the tool server could not be reached ({type(error).__name__})")


def media_type_of(response: httpx.Response) -> str:
    return response.headers.get("content-type", "").partition(";")[0].strip().lower()


class McpSession:
    """One session with one server, opened by ``session``."""

    def __init__(self, http: httpx.AsyncClient, url: str, headers: Mapping[str, str]) -> None:
        self._http = http
        self._url = url
        self._headers = {"accept": ACCEPT, **headers}
        self._next_id = 0

    async def request(self, method: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """The result of ``method``; ``McpError`` for anything else."""
        self._next_id += 1
        request_id = self._next_id
        body: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            body["params"] = dict(params)
        try:
            async with self._http.stream(
                "POST", self._url, json=body, headers=self._headers
            ) as response:
                self._check(response)
                if method == "initialize" and "mcp-session-id" in response.headers:
                    self._headers["mcp-session-id"] = response.headers["mcp-session-id"]
                media_type = media_type_of(response)
                if media_type == JSON:
                    return self._only_answer(await self._body(response), request_id)
                if media_type == EVENT_STREAM:
                    return await self._streamed_answer(response, request_id)
                raise McpError("the tool server answered neither JSON nor server-sent events")
        except httpx.HTTPError as error:
            raise unreached(error) from error

    async def notify(self, method: str) -> None:
        body = {"jsonrpc": "2.0", "method": method}
        try:
            response = await self._http.post(self._url, json=body, headers=self._headers)
        except httpx.HTTPError as error:
            raise unreached(error) from error
        self._check(response)

    async def end(self) -> None:
        """Tell a server that named the session that it is over. A server may refuse, and a
        server that keeps no session has nothing to end: neither is anything of ours."""
        if "mcp-session-id" not in self._headers:
            return
        try:
            await self._http.delete(self._url, headers=self._headers, timeout=END_SECONDS)
        except httpx.HTTPError:
            pass

    def started(self, result: Mapping[str, Any]) -> None:
        """Speak the version ``initialize`` settled on from now on."""
        version = result.get("protocolVersion")
        if isinstance(version, str) and version.isprintable():
            self._headers["mcp-protocol-version"] = version

    @staticmethod
    def _check(response: httpx.Response) -> None:
        if not response.is_success:
            raise McpError(f"the tool server answered HTTP {response.status_code}")

    @staticmethod
    async def _body(response: httpx.Response) -> str:
        chunks: list[bytes] = []
        size = 0
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > MAX_ANSWER_BYTES:
                raise McpError("the tool server's answer is too long")
            chunks.append(chunk)
        try:
            return b"".join(chunks).decode("utf-8")
        except UnicodeDecodeError as error:
            raise McpError("the tool server's answer is not UTF-8") from error

    @staticmethod
    def _only_answer(text: str, request_id: int) -> dict[str, Any]:
        found = answer_in(parsed(text), request_id)
        if found is None:
            raise McpError("the tool server did not answer the request")
        return found

    @staticmethod
    async def _streamed_answer(response: httpx.Response, request_id: int) -> dict[str, Any]:
        """The answer among the events, read until it comes: a server may send notifications
        of its own first, and may hold the stream open after it."""
        reader = EventReader()
        size = 0
        async for text in response.aiter_text():
            size += len(text)
            if size > MAX_ANSWER_BYTES:
                raise McpError("the tool server's answer is too long")
            for data in reader.feed(text):
                found = answer_in(parsed(data), request_id)
                if found is not None:
                    return found
        for data in reader.close():
            found = answer_in(parsed(data), request_id)
            if found is not None:
                return found
        raise McpError("the tool server did not answer the request")


@asynccontextmanager
async def session(
    url: str, headers: Mapping[str, str], timeout_seconds: float
) -> AsyncIterator[McpSession]:
    """A session with the server at ``url``, initialised, ended when the block ends. Each read
    or write waits ``timeout_seconds`` at most; the caller bounds the whole."""
    async with httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=False) as http:
        opened = McpSession(http, url, headers)
        started = await opened.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "robinauts", "version": "0"},
            },
        )
        opened.started(started)
        await opened.notify("notifications/initialized")
        try:
            yield opened
        finally:
            await opened.end()
