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
read off the wire as it arrives, up to ``MAX_RESPONSE_BYTES`` and refused past
it (an event stream is read only as far as our answer, so a server that keeps
it open holds nothing up), a listing is followed for at most ``MAX_PAGES``
pages inside ``LISTING_SECONDS``, a call is bounded whole by the server's
``timeout_seconds``, and a result's text is cut to what a part may hold with
a note saying how much was left out. What a server lists and answers
is attacker-influenced text on its way to a model and a browser; it is carried
as data and checked by the domain's records on the way in.
"""

from __future__ import annotations

import asyncio
import base64
import functools
import json
import logging
import ssl
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
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

SUPPORTED_VERSIONS = frozenset({"2025-06-18"})
"""The revisions of Streamable HTTP this client can hold a conversation in.

One: the revision before it allowed JSON-RPC batches, which this client does
not read, so a server that answers with it is refused rather than half-read.
"""

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
"""How many pages of a listing are followed before the listing is refused."""

LISTING_SECONDS = 60.0
"""How long listing a server's tools may take in all: the session, every page, the goodbye.

The per-read timeout of the client bounds one read and not a server that
drips; this bounds the question. A call's whole-call bound is the server's
own ``timeout_seconds``.
"""

QUIET_CLIENT_LOGGERS = ("httpx", "httpcore")
"""The loggers held at ``WARNING`` when the adapter is built, and why.

``httpx`` logs every request's URL at ``INFO`` and ``httpcore`` logs the
connection's traffic at ``DEBUG``, response headers among them -- a server's
session id, and whatever else a server puts there. What a tool server is
asked and answers is conversation content by another name, and the platform's
logs never carry that (``docs/specs/agents.md``, "Tools"). The sign-in adapter
shares this client library and these loggers; pinning them here pins them for
both.
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
        doing = "listing its tools"
        listed: list[ListedTool] = []
        try:
            async with asyncio.timeout(LISTING_SECONDS), self._session(server, doing) as session:
                cursor: str | None = None
                for _ in range(MAX_PAGES):
                    params: dict[str, Any] = {"cursor": cursor} if cursor else {}
                    page = await session.request("tools/list", params, doing)
                    if isinstance(page, _Refusal):
                        raise ToolServerError(
                            f"tool server {server.id!r} would not list its tools: {page.text}"
                        )
                    tools = page.get("tools")
                    if not isinstance(tools, list):
                        raise ToolServerError(
                            f"tool server {server.id!r} answered {doing} without a list of tools"
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
        except TimeoutError:
            raise ToolServerError(
                f"tool server {server.id!r} took longer than {LISTING_SECONDS:g} seconds {doing}"
            ) from None

    async def call_tool(
        self, server: ToolServerConfig, name: str, arguments: Mapping[str, Any]
    ) -> ToolResult:
        doing = f"calling {name!r}"
        try:
            # The server's bound is over the whole call: the session it takes,
            # the request, and the goodbye.
            async with (
                asyncio.timeout(server.timeout_seconds),
                self._session(server, doing) as session,
            ):
                answer = await session.request(
                    "tools/call", {"name": name, "arguments": dict(arguments)}, doing, softly=True
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
        if isinstance(answer, _Unanswered):
            return ToolResult(
                f"the server answered the call to {name!r} with HTTP {answer.status}",
                is_error=True,
            )
        return _result_of(answer)

    @asynccontextmanager
    async def _session(self, server: ToolServerConfig, doing: str) -> AsyncIterator[_Session]:
        session = _Session(self._client, server, functools.partial(self._authorization, server))
        try:
            await session.open(doing)
            yield session
        finally:
            await session.close()

    def _authorization(self, server: ToolServerConfig) -> str | None:
        """The ``Authorization`` header's value for that server, built when a request is.

        ``None`` for a server with no ``auth``: a public server is sent no
        header at all, rather than an empty one it might read as a credential.
        """
        if server.auth is ToolServerAuth.NONE:
            return None
        secret = self._secrets.secret_for(server.id)
        if server.auth is ToolServerAuth.BASIC:
            pair = base64.b64encode(f"{server.user}:{secret}".encode()).decode("ascii")
            return f"Basic {pair}"
        return f"Bearer {secret}"


@dataclass(frozen=True, slots=True)
class _Refusal:
    """A JSON-RPC error the server answered a request with: its code and message, as text."""

    text: str


@dataclass(frozen=True, slots=True)
class _Unanswered:
    """An HTTP status a server answered a call with instead of the protocol."""

    status: int


PROTOCOL_ERRORS = frozenset({-32700, -32600, -32601})
"""The JSON-RPC codes that say the server could not read our message, or has no such method.

Nothing the model could act on -- the fault is between the client and the
server -- so a call answered with one of these is a ``ToolServerError``, where
every other code (an unknown tool, bad arguments, the tool's own failure) is a
result the model is told.
"""


@dataclass(repr=False)
class _Session:
    """One MCP session: opened with ``initialize``, one request, closed with ``DELETE``.

    **No credential is held here.** ``authorize`` builds the header's value
    when a request is built, and ``repr`` is turned off, so that neither a
    traceback's locals nor a log line formatting a session can carry the
    secret (the port's promise, ``robinauts.ports.tool_servers``).
    """

    client: httpx.AsyncClient
    server: ToolServerConfig
    authorize: Callable[[], str | None]
    session_id: str | None = None
    version: str = PROTOCOL_VERSION
    next_id: int = 1

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
        assert not isinstance(answer, _Unanswered)  # not asked for softly
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
        """Say the session is over, if the server gave one -- briefly, and not while cancelled.

        A ``DELETE`` bounded by the connect timeout: the server that has just
        not answered is not one to wait on. Skipped when this task is being
        cancelled -- a cancellation is let through at once, and the server
        expires the session on its own -- and when the server does not take a
        ``DELETE`` (405) or has forgotten the session (404), which say nothing.
        """
        if self.session_id is None:
            return
        task = asyncio.current_task()
        if task is not None and task.cancelling():
            return
        try:
            async with asyncio.timeout(CONNECT_TIMEOUT_SECONDS):
                request = self.client.build_request(
                    "DELETE", self.server.url, headers=self._headers(opening=False)
                )
                response = await self.client.send(request, stream=True, follow_redirects=False)
                await response.aclose()
        except (TimeoutError, httpx.HTTPError, httpx.InvalidURL, OSError):
            return

    async def request(
        self,
        method: str,
        params: Mapping[str, Any],
        doing: str,
        *,
        opening: bool = False,
        softly: bool = False,
    ) -> Mapping[str, Any] | _Refusal | _Unanswered:
        """Send one request and read its result, or what the server answered instead.

        ``softly`` is a call: a status that is not the protocol's comes back
        as ``_Unanswered`` for the model to be told, where any other request
        fails on it.
        """
        request_id = self.next_id
        self.next_id += 1
        message = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": dict(params)}
        response = await self._post(message, doing, opening=opening, softly=softly)
        if isinstance(response, _Unanswered):
            return response
        assert response is not None  # a request is answered or fails
        if response.get("id") != request_id:
            raise ToolServerError(
                f"tool server {self.server.id!r} answered {doing} with a message for another"
                f" request"
            )
        if "error" in response:
            error = response["error"]
            code = error.get("code") if isinstance(error, Mapping) else None
            text = error.get("message") if isinstance(error, Mapping) else None
            if code in PROTOCOL_ERRORS or not isinstance(error, Mapping):
                raise ToolServerError(
                    f"tool server {self.server.id!r} could not take the request {doing}:"
                    f" JSON-RPC error {_shown(code)}"
                )
            return _Refusal(
                f"{_shown(code)} {_shown(text) if isinstance(text, str) else ''}".strip()
            )
        result = response.get("result")
        if not isinstance(result, Mapping):
            raise ToolServerError(f"tool server {self.server.id!r} answered {doing} with no result")
        return result

    def _headers(self, *, opening: bool) -> dict[str, str]:
        """Every request's headers: the protocol's, the credential, the session.

        Set on the request and not left to the client, so that what the
        server is told does not depend on which client the adapter was built
        with (a test hands one in). The protocol revision goes on every
        request after ``initialize`` agreed it.
        """
        headers = {
            "Accept": "application/json, text/event-stream",
            "Accept-Encoding": "identity",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        }
        authorization = self.authorize()
        if authorization is not None:
            headers["Authorization"] = authorization
        if self.session_id is not None:
            headers[SESSION_HEADER] = self.session_id
        if not opening:
            headers[VERSION_HEADER] = self.version
        return headers

    async def _post(
        self,
        message: Mapping[str, Any],
        doing: str,
        *,
        notify: bool = False,
        opening: bool = False,
        softly: bool = False,
    ) -> Mapping[str, Any] | _Unanswered | None:
        """One message to the server, and what came back, read as it arrives and bounded.

        A failure is **raised after** the request and the response are out of
        every frame and with nothing chained to it: what a failed request
        carries is the request, credential included, and neither a traceback's
        locals nor an exception's ``__context__`` is a place for one.
        """
        request = self.client.build_request(
            "POST",
            self.server.url,
            content=json.dumps(message).encode(),
            headers=self._headers(opening=opening),
        )
        failure: ToolServerError | None = None
        response: httpx.Response | None = None
        try:
            try:
                response = await self.client.send(request, stream=True, follow_redirects=False)
            except (httpx.HTTPError, httpx.InvalidURL, OSError) as exc:
                # The exception's type, never its message.
                failure = ToolServerError(
                    f"tool server {self.server.id!r} cannot be reached while {doing}:"
                    f" {type(exc).__name__}"
                )
            if response is not None:
                status = response.status_code
                if status in (401, 403):
                    failure = ToolServerError(
                        f"tool server {self.server.id!r} refused the credential while {doing}"
                        f" ({status})"
                    )
                elif not 200 <= status < 300:
                    if softly:
                        return _Unanswered(status)
                    failure = ToolServerError(
                        f"tool server {self.server.id!r} answered {doing} with HTTP {status}"
                    )
                else:
                    if opening and (given := response.headers.get(SESSION_HEADER)):
                        self.session_id = given
                    if notify:
                        return None
                    kind = response.headers.get("content-type", "").split(";")[0].strip().lower()
                    if kind == "text/event-stream":
                        return await _answer_in(response, message["id"], self.server.id, doing)
                    if kind == "application/json":
                        return _message_of(
                            await _body_of(response, self.server.id, doing), self.server.id, doing
                        )
                    failure = ToolServerError(
                        f"tool server {self.server.id!r} answered {doing} as"
                        f" '{_shown(kind) or 'nothing'}', not as JSON or an event stream"
                    )
        finally:
            del request
            if response is not None:
                await response.aclose()
        assert failure is not None
        raise failure


def _declared_too_long(response: httpx.Response, server_id: str, doing: str) -> None:
    """Refuse an answer that says it is over the bound, or comes encoded, before reading it."""
    encoding = response.headers.get("content-encoding", "").strip().lower()
    if encoding not in ("", "identity"):
        raise ToolServerError(
            f"tool server {server_id!r} answered {doing} with a content encoding this"
            f" deployment did not ask for"
        )
    declared = response.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > MAX_RESPONSE_BYTES:
        raise ToolServerError(
            f"tool server {server_id!r} answered {doing} with more than"
            f" {MAX_RESPONSE_BYTES} bytes"
        )


async def _body_of(response: httpx.Response, server_id: str, doing: str) -> bytes:
    """The body, read off the wire up to the bound and refused past it.

    ``aiter_bytes`` rather than ``aiter_raw``: with the encodings refused
    above they yield the same bytes, and only the former serves a response a
    transport handed over already read (as a test's does).
    """
    _declared_too_long(response, server_id, doing)
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


async def _answer_in(
    response: httpx.Response, request_id: object, server_id: str, doing: str
) -> Mapping[str, Any]:
    """The response to our request, read off an event stream as it arrives.

    Notifications and messages for other requests are passed over; the read
    stops at our response, whether or not the server then closes the stream
    (the protocol says it should), and is bounded on the way.
    """
    _declared_too_long(response, server_id, doing)
    events = _Events()
    size = 0
    async for chunk in response.aiter_bytes():
        size += len(chunk)
        if size > MAX_RESPONSE_BYTES:
            raise ToolServerError(
                f"tool server {server_id!r} answered {doing} with more than"
                f" {MAX_RESPONSE_BYTES} bytes"
            )
        for data in events.feed(chunk):
            found = _our_response(data, request_id)
            if found is not None:
                return found
    for data in events.finish():
        found = _our_response(data, request_id)
        if found is not None:
            return found
    raise ToolServerError(
        f"tool server {server_id!r} closed the event stream for {doing} without answering"
    )


def _our_response(data: str, request_id: object) -> Mapping[str, Any] | None:
    try:
        message = json.loads(data)
    except (ValueError, RecursionError):
        return None
    if (
        isinstance(message, Mapping)
        and message.get("id") == request_id
        and ("result" in message or "error" in message)
    ):
        return message
    return None


class _Events:
    """A server-sent event stream, parsed as its bytes arrive: the ``data`` of each event.

    Lines end in LF, CRLF or CR; a leading byte-order mark is passed over;
    ``data`` lines of one event are joined with LF; ``event``, ``id``,
    ``retry`` and comments are read past, since what is wanted is the message.
    """

    def __init__(self) -> None:
        self._buffer = b""
        self._data: list[str] = []
        self._begun = False

    def feed(self, chunk: bytes) -> list[str]:
        self._buffer += chunk
        if not self._begun and len(self._buffer) >= 3:
            self._buffer = self._buffer.removeprefix(b"\xef\xbb\xbf")
            self._begun = True
        events: list[str] = []
        while True:
            cut = _line_end(self._buffer)
            if cut is None:
                return events
            line_end, next_start = cut
            line = self._buffer[:line_end].decode("utf-8", errors="replace")
            self._buffer = self._buffer[next_start:]
            finished = self._line(line)
            if finished is not None:
                events.append(finished)

    def finish(self) -> list[str]:
        """What a stream that ended without a final blank line still carried."""
        events = self.feed(b"\n") if self._buffer else []
        if self._data:
            events.append("\n".join(self._data))
            self._data = []
        return events

    def _line(self, line: str) -> str | None:
        if not line:
            if not self._data:
                return None
            event, self._data = "\n".join(self._data), []
            return event
        if line.startswith(":"):
            return None
        name, _, value = line.partition(":")
        if name == "data":
            self._data.append(value[1:] if value.startswith(" ") else value)
        return None


def _line_end(buffer: bytes) -> tuple[int, int] | None:
    """Where the first whole line in ``buffer`` ends, and where the next begins."""
    at = len(buffer)
    for mark in (b"\r\n", b"\n", b"\r"):
        found = buffer.find(mark)
        if found != -1 and found < at:
            at, width = found, len(mark)
    if at == len(buffer):
        return None
    if buffer[at : at + 1] == b"\r" and at + 1 == len(buffer):
        # A CR at the very end may be the first half of a CRLF: wait for more.
        return None
    return at, at + width


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
        # The note's own length comes off what is kept, so the count is of
        # the cut actually made; a count of every character is the longest
        # the note can be, which is what keeps the whole inside the bound.
        kept = MAX_PART_CHARS - len(TRUNCATED_NOTE.format(count=len(text)))
        text = text[:kept] + TRUNCATED_NOTE.format(count=len(text) - kept)
    return ToolResult(text, is_error=result.get("isError") is True)


def _shown(value: object) -> str:
    """A value from the wire, as a short piece of printable text a message may carry."""
    text = str(value) if value is not None else ""
    text = "".join(character if character.isprintable() else "?" for character in text)
    return text[:80]
