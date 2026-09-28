# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""A client of our own for MCP over Streamable HTTP: three calls, over ``httpx``.

The one ``ToolServers`` implementation (``robinauts.ports.tool_servers``), and
the only module in the platform that speaks the Model Context Protocol. The
MCP Python SDK is not adopted -- its tree fails the licence gate
(``DEPENDENCIES.md``, "Known exclusions") -- so what a client needs is written
here: JSON-RPC 2.0 over Streamable HTTP, which is one ``POST`` per message to
the server's one URL, answered either as a JSON body or as a
``text/event-stream`` carrying the JSON-RPC response, with a session id the
server may hand out on ``initialize`` and expects back on every request
(``docs/specs/agents.md``, "Tools"; the protocol is at modelcontextprotocol.io,
revision ``PROTOCOL_VERSION``).

**One session per question.** ``list_tools`` and ``call_tool`` each open a
session -- ``initialize``, the ``initialized`` notification, the one request,
and a ``DELETE`` to say the session is over -- on one connection pool held for
the life of the adapter. Nothing is remembered between questions, which is
what lets the application ask a run's servers in parallel and a process hold
no state about a server (``docs/layout.md``; a bounded cache in front of
``tools/list`` is the plan's backlog).

**Pinned as the vendor clients are** (``docs/layout.md``): the endpoint is the
configuration's and nothing else; the credential is a header, built from what
start-up read (``ToolServerSecrets``) and never from the environment; there
are no retries the application cannot see; redirects are not followed, so the
credential is never posted to whoever answered a redirect; the proxy is the
operator's ``HTTPS_PROXY``; and ``httpx``'s and ``httpcore``'s loggers, which
write request lines and headers -- the credential among them -- at ``DEBUG``,
are held at ``WARNING`` when the adapter is built (``quiet_client_logging``).
Nothing here writes a tool's arguments or results to a log either.

**A tool's error is a result, not a failure** (``docs/specs/runs.md``,
"Tools"): a server answering ``isError``, a JSON-RPC error on ``tools/call``
(an unknown tool, bad arguments) and a call that runs out of the server's
``timeout_seconds`` all come back as a ``ToolResult`` marked as an error, and
the model is told. A server that cannot be reached, refuses the credential,
answers something that is not the protocol, or will not list its tools is a
``ToolServerError`` naming the server -- by its configured id and what was
asked, never by what was sent.

**Everything read off the wire is bounded before it is parsed**: a body is
read up to ``MAX_RESPONSE_BYTES`` and refused past it, a listing is followed
for at most ``MAX_PAGES`` pages, and a result's text is cut to what a part may
hold with a note saying how much was left out. What a server lists and answers
is attacker-influenced text on its way to a model and a browser; it is carried
as data and checked by the domain's records on the way in.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import ssl
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx

from robinauts.adapters.config_file import ToolServerSecrets
from robinauts.domain import (
    MAX_PART_CHARS,
    InvalidValueError,
    ListedTool,
    ToolResult,
    ToolServerAuth,
    ToolServerConfig,
    ToolServerError,
    clean_text,
)
from robinauts.ports import ToolServers

_log = logging.getLogger(__name__)

PROTOCOL_VERSION = "2025-06-18"
"""The protocol revision this client speaks, offered on ``initialize``.

A server answers with the revision it will use; one this client does not
know is refused rather than guessed at (``SUPPORTED_VERSIONS``).
"""

SUPPORTED_VERSIONS = frozenset({"2025-06-18", "2025-03-26"})
"""The revisions of Streamable HTTP this client can hold a conversation in."""

CLIENT_INFO: Mapping[str, str] = {"name": "robinauts", "version": "0.1.0"}
"""Who the server is told is calling."""

USER_AGENT = "robinauts (+https://github.com/open-shipyard/robinauts)"

CONNECT_TIMEOUT_SECONDS = 5.0
"""How long opening a connection to a server may take."""

READ_TIMEOUT_SECONDS = 30.0
"""How long one read off a server may take, for the requests that are not a tool call.

A tool call's own bound is the server's ``timeout_seconds``, over the whole
call; this is the client's, per read, for ``initialize`` and ``tools/list``.
"""

MAX_RESPONSE_BYTES = 8 * 1024 * 1024
"""The most that is read of one answer: a large server's listing fits; a runaway does not."""

MAX_PAGES = 100
"""How many pages of ``tools/list`` are followed before the listing is refused."""

QUIET_CLIENT_LOGGERS = ("httpx", "httpcore")
"""The loggers held at ``WARNING`` when the adapter is built, and why.

``httpx`` logs every request line at ``INFO`` and ``httpcore`` logs request
headers at ``DEBUG`` -- the ``Authorization`` header among them. The sign-in
adapter shares this client library and these loggers; pinning them here pins
them for both, which is the same promise (``docs/specs/core.md``).
"""

QUIET_CLIENT_LEVEL = logging.WARNING

SESSION_HEADER = "Mcp-Session-Id"
VERSION_HEADER = "MCP-Protocol-Version"

LEFT_OUT_NOTE = "[{what} left out]"
"""The text a result part of another kind becomes (``docs/specs/agents.md``)."""

TRUNCATED_NOTE = "\n[{count} more characters left out]"


def quiet_client_logging() -> None:
    """Hold the HTTP client's loggers at ``WARNING`` unless somebody set them stricter."""
    for name in QUIET_CLIENT_LOGGERS:
        logger = logging.getLogger(name)
        if logger.level == logging.NOTSET or logger.level < QUIET_CLIENT_LEVEL:
            logger.setLevel(QUIET_CLIENT_LEVEL)


def open_client(*, trust_env: bool = True) -> httpx.AsyncClient:
    """The HTTP client the adapter reaches tool servers with: verified, bounded, no redirects."""
    context = httpx.create_ssl_context(trust_env=trust_env)
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    return httpx.AsyncClient(
        verify=context,
        timeout=httpx.Timeout(
            connect=CONNECT_TIMEOUT_SECONDS,
            read=READ_TIMEOUT_SECONDS,
            write=READ_TIMEOUT_SECONDS,
            pool=CONNECT_TIMEOUT_SECONDS,
        ),
        follow_redirects=False,
        headers={"User-Agent": USER_AGENT},
        trust_env=trust_env,
    )


ClientFactory = Callable[..., httpx.AsyncClient]


class McpToolServers(ToolServers):
    """The port over Streamable HTTP, for every server the configuration names."""

    def __init__(
        self,
        secrets: ToolServerSecrets,
        *,
        trust_env: bool = True,
        client_for: ClientFactory = open_client,
    ) -> None:
        quiet_client_logging()
        self._secrets = secrets
        self._client = client_for(trust_env=trust_env)

    async def aclose(self) -> None:
        """Close the connection pool. The deployment calls it when it closes."""
        await self._client.aclose()

    async def list_tools(self, server: ToolServerConfig) -> list[ListedTool]:
        listed: list[ListedTool] = []
        async with self._session(server, "listing its tools") as session:
            cursor: str | None = None
            for _ in range(MAX_PAGES):
                params: dict[str, Any] = {"cursor": cursor} if cursor else {}
                page = await session.request("tools/list", params, "listing its tools")
                if isinstance(page, _Refusal):
                    raise ToolServerError(
                        f"tool server {server.id!r} would not list its tools: {page.text}"
                    )
                tools = page.get("tools")
                if not isinstance(tools, list):
                    raise ToolServerError(
                        f"tool server {server.id!r} answered tools/list without a list of tools"
                    )
                for position, entry in enumerate(tools):
                    tool = _listed(entry)
                    if tool is None:
                        _log.warning(
                            "tool server %r listed a tool this build cannot carry (entry %d),"
                            " left out",
                            server.id,
                            position,
                        )
                    else:
                        listed.append(tool)
                cursor = page.get("nextCursor")
                if not isinstance(cursor, str) or not cursor:
                    return listed
            raise ToolServerError(
                f"tool server {server.id!r} listed more than {MAX_PAGES} pages of tools"
            )

    async def call_tool(
        self, server: ToolServerConfig, name: str, arguments: Mapping[str, Any]
    ) -> ToolResult:
        async with self._session(server, f"calling {name!r}") as session:
            try:
                async with asyncio.timeout(server.timeout_seconds):
                    answer = await session.request(
                        "tools/call",
                        {"name": name, "arguments": dict(arguments)},
                        f"calling {name!r}",
                    )
            except TimeoutError:
                return ToolResult(
                    f"the call to {name!r} ran out of its {server.timeout_seconds:g} seconds",
                    is_error=True,
                )
        if isinstance(answer, _Refusal):
            return ToolResult(
                f"the server refused the call to {name!r}: {answer.text}", is_error=True
            )
        return _result_of(answer)

    @asynccontextmanager
    async def _session(self, server: ToolServerConfig, doing: str) -> AsyncIterator[_Session]:
        session = _Session(self._client, server, self._authorization(server))
        try:
            await session.open(doing)
            yield session
        finally:
            await session.close()

    def _authorization(self, server: ToolServerConfig) -> str:
        secret = self._secrets.secret_for(server.id)
        if server.auth is ToolServerAuth.BASIC:
            pair = base64.b64encode(f"{server.user}:{secret}".encode()).decode("ascii")
            return f"Basic {pair}"
        return f"Bearer {secret}"


@dataclass(frozen=True, slots=True)
class _Refusal:
    """A JSON-RPC error the server answered a request with: its code and message, as text."""

    text: str


@dataclass
class _Session:
    """One MCP session: opened with ``initialize``, one request, closed with ``DELETE``."""

    client: httpx.AsyncClient
    server: ToolServerConfig
    authorization: str
    session_id: str | None = None
    version: str = PROTOCOL_VERSION
    next_id: int = 1
    _extra: dict[str, str] = field(default_factory=dict)

    async def open(self, doing: str) -> None:
        answer = await self.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": dict(CLIENT_INFO),
            },
            doing,
            opening=True,
        )
        if isinstance(answer, _Refusal):
            raise ToolServerError(
                f"tool server {self.server.id!r} refused to initialize a session: {answer.text}"
            )
        version = answer.get("protocolVersion")
        if version not in SUPPORTED_VERSIONS:
            raise ToolServerError(
                f"tool server {self.server.id!r} speaks protocol revision"
                f" '{_shown(version)}', which this build does not"
            )
        self.version = str(version)
        await self._post(
            {"jsonrpc": "2.0", "method": "notifications/initialized"}, doing, notify=True
        )

    async def close(self) -> None:
        if self.session_id is None:
            return
        try:
            response = await self.client.delete(
                self.server.url, headers=self._headers(), follow_redirects=False
            )
            await response.aclose()
        except (httpx.HTTPError, httpx.InvalidURL, OSError):
            # The session is over either way; a server that does not take a
            # DELETE (405) or has forgotten the session (404) says so and
            # nothing follows from it.
            return

    async def request(
        self, method: str, params: Mapping[str, Any], doing: str, *, opening: bool = False
    ) -> Mapping[str, Any] | _Refusal:
        """Send one request and read its result, or the error the server answered with."""
        request_id = self.next_id
        self.next_id += 1
        message = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": dict(params)}
        response = await self._post(message, doing, opening=opening)
        assert response is not None
        if not isinstance(response.get("id"), int) or response["id"] != request_id:
            raise ToolServerError(
                f"tool server {self.server.id!r} answered {doing} with a message for another"
                f" request"
            )
        if "error" in response:
            error = response["error"]
            code = error.get("code") if isinstance(error, Mapping) else None
            text = error.get("message") if isinstance(error, Mapping) else None
            return _Refusal(
                f"{_shown(code)} {_shown(text) if isinstance(text, str) else ''}".strip()
            )
        result = response.get("result")
        if not isinstance(result, Mapping):
            raise ToolServerError(f"tool server {self.server.id!r} answered {doing} with no result")
        return result

    def _headers(self) -> dict[str, str]:
        """Every request's headers: the protocol's, the credential, the session.

        Set on the request and not left to the client, so that what the
        server is told does not depend on which client the adapter was built
        with (a test hands one in).
        """
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
            "Authorization": self.authorization,
        }
        if self.session_id is not None:
            headers[SESSION_HEADER] = self.session_id
        return headers

    async def _post(
        self, message: Mapping[str, Any], doing: str, *, notify: bool = False, opening: bool = False
    ) -> Mapping[str, Any] | None:
        headers = self._headers()
        if not opening:
            headers[VERSION_HEADER] = self.version
        try:
            response = await self.client.post(
                self.server.url, content=json.dumps(message).encode(), headers=headers
            )
        except (httpx.HTTPError, httpx.InvalidURL, OSError) as exc:
            # The exception's type, never its message: what a failed request
            # carries is the request, credential included.
            raise ToolServerError(
                f"tool server {self.server.id!r} cannot be reached while {doing}:"
                f" {type(exc).__name__}"
            ) from None
        try:
            if response.status_code in (401, 403):
                raise ToolServerError(
                    f"tool server {self.server.id!r} refused the credential while {doing}"
                    f" ({response.status_code})"
                )
            if not 200 <= response.status_code < 300:
                raise ToolServerError(
                    f"tool server {self.server.id!r} answered {doing} with HTTP"
                    f" {response.status_code}"
                )
            if opening and (given := response.headers.get(SESSION_HEADER)):
                self.session_id = given
            if notify:
                return None
            body = await _bounded(response, self.server.id, doing)
        finally:
            await response.aclose()
        kind = response.headers.get("content-type", "").split(";")[0].strip().lower()
        if kind == "text/event-stream":
            return _response_in(_events_of(body), message["id"], self.server.id, doing)
        if kind == "application/json":
            return _message_of(body, self.server.id, doing)
        raise ToolServerError(
            f"tool server {self.server.id!r} answered {doing} as '{_shown(kind) or 'nothing'}',"
            f" not as JSON or an event stream"
        )


async def _bounded(response: httpx.Response, server_id: str, doing: str) -> bytes:
    """The body, read up to the bound and refused past it."""
    chunks: list[bytes] = []
    size = 0
    async for chunk in response.aiter_bytes():
        size += len(chunk)
        if size > MAX_RESPONSE_BYTES:
            raise ToolServerError(
                f"tool server {server_id!r} answered {doing} with more than"
                f" {MAX_RESPONSE_BYTES} bytes"
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _message_of(body: bytes, server_id: str, doing: str) -> Mapping[str, Any]:
    try:
        message = json.loads(body)
    except (ValueError, RecursionError):
        raise ToolServerError(
            f"tool server {server_id!r} answered {doing} with something that is not JSON"
        ) from None
    if not isinstance(message, Mapping) or message.get("jsonrpc") != "2.0":
        raise ToolServerError(
            f"tool server {server_id!r} answered {doing} with something that is not a"
            f" JSON-RPC message"
        )
    return message


def _events_of(body: bytes) -> list[str]:
    """The ``data`` of every event in a server-sent event stream, in order."""
    events: list[str] = []
    data: list[str] = []
    for raw in body.decode("utf-8", errors="replace").split("\n"):
        line = raw.rstrip("\r")
        if not line:
            if data:
                events.append("\n".join(data))
                data = []
            continue
        if line.startswith(":"):
            continue
        name, _, value = line.partition(":")
        if name == "data":
            data.append(value[1:] if value.startswith(" ") else value)
    if data:
        events.append("\n".join(data))
    return events


def _response_in(
    events: list[str], request_id: int, server_id: str, doing: str
) -> Mapping[str, Any]:
    """The response to our request among the stream's messages; the rest is passed over."""
    for data in events:
        try:
            message = json.loads(data)
        except (ValueError, RecursionError):
            continue
        if (
            isinstance(message, Mapping)
            and message.get("id") == request_id
            and ("result" in message or "error" in message)
        ):
            return message
    raise ToolServerError(
        f"tool server {server_id!r} closed the event stream for {doing} without answering"
    )


def _listed(entry: object) -> ListedTool | None:
    """One entry of ``tools/list`` as the platform's record, or ``None`` for a shape it is not."""
    if not isinstance(entry, Mapping) or not isinstance(entry.get("name"), str):
        return None
    description = entry.get("description", "")
    schema = entry.get("inputSchema", {})
    annotations = entry.get("annotations", {})
    try:
        return ListedTool(
            name=entry["name"],
            description=description if isinstance(description, str) else "",
            input_schema=schema if isinstance(schema, Mapping) else {},
            annotations=annotations if isinstance(annotations, Mapping) else {},
        )
    except InvalidValueError:
        return None


def _result_of(result: Mapping[str, Any]) -> ToolResult:
    """A ``tools/call`` result as text: the text parts, notes for the rest, bounded."""
    pieces: list[str] = []
    content = result.get("content")
    for item in content if isinstance(content, list) else []:
        if not isinstance(item, Mapping):
            continue
        kind = item.get("type")
        if kind == "text" and isinstance(item.get("text"), str):
            pieces.append(item["text"])
        elif kind in ("image", "audio"):
            pieces.append(LEFT_OUT_NOTE.format(what=f"an {kind} ({_shown(item.get('mimeType'))})"))
        elif kind == "resource":
            resource = item.get("resource")
            text = resource.get("text") if isinstance(resource, Mapping) else None
            if isinstance(text, str):
                pieces.append(text)
            else:
                uri = resource.get("uri") if isinstance(resource, Mapping) else None
                pieces.append(LEFT_OUT_NOTE.format(what=f"a resource ({_shown(uri)})"))
        elif kind == "resource_link":
            pieces.append(LEFT_OUT_NOTE.format(what=f"a link to ({_shown(item.get('uri'))})"))
        else:
            pieces.append(LEFT_OUT_NOTE.format(what=f"content of kind ({_shown(kind)})"))
    if not pieces and isinstance(result.get("structuredContent"), Mapping):
        pieces.append(json.dumps(result["structuredContent"], ensure_ascii=False))
    text = clean_text("\n\n".join(pieces))
    if len(text) > MAX_PART_CHARS:
        note = TRUNCATED_NOTE.format(count=len(text) - MAX_PART_CHARS)
        text = text[: MAX_PART_CHARS - len(note)] + note
    return ToolResult(text, is_error=result.get("isError") is True)


def _shown(value: object) -> str:
    """A value from the wire, as a short piece of printable text a message may carry."""
    text = str(value) if value is not None else ""
    text = "".join(character if character.isprintable() else "?" for character in text)
    return text[:80]
