# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""neorc's manager, served on this server's own origin under ``PREFIX``, for the interface's
Flows page (``docs/architecture/orchestration.md``, "The Flows page").

Only in the local development mode with an ``[orchestration]`` table: the manager is the one
``robinauts start`` ran beside itself, on the loopback interface with no authentication, and
``create_app`` mounts this proxy only then. Without it, nothing answers under ``PREFIX``.

Everything under ``PREFIX`` goes to the manager with ``PREFIX`` taken off: its UI at
``/neorc/ui/``, and its API right above it, where that UI looks for it. The answer streams
back as it comes, so the UI's 20-second long polls of the event log pass through, and a client
that goes away cancels the request upstream.

The request reaches the manager without this server's cookies, without ``Authorization`` and
without ``Origin``: neorc has no business with this server's credentials, and the ``Guard``
outside has already checked the ``Host`` and the ``Origin``. The answer comes back without
``Set-Cookie``, so neorc can set nothing on this origin, and without the hop-by-hop headers.

An answer of the UI keeps neorc's own Content-Security-Policy with two changes
(``ui_policy``): ``frame-ancestors 'self'``, so that this server's interface, and nothing else,
may frame it; and ``connect-src`` narrowed from the whole origin to ``PREFIX``, so that the
UI's own requests reach the manager and not ``/api/``. ``x-frame-options: SAMEORIGIN`` goes
with it. Every other answer carries this server's ``SECURITY_HEADERS`` like any other.

This puts neorc's bundle, and every package it bundles, on this server's origin, which
Robinauts' dependency vetting does not cover. The page and its frame share an origin, so a
script in the frame can reach the page and, through it, ``/api/``: the narrowed policy and the
frame's ``sandbox`` stop the obvious ways, not a determined one. That is the trade-off of this
variant, and why it exists in the local development mode alone.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from http.cookiejar import CookieJar, DefaultCookiePolicy
from urllib.parse import urlsplit

import httpx
from starlette.responses import JSONResponse
from starlette.types import Message, Receive, Scope, Send

PREFIX = "/neorc"
"""Where this server answers for the manager."""
UI_PATH = "/ui"
"""Where the manager serves its UI, below ``PREFIX``."""

MAX_BODY_BYTES = 8 * 1024 * 1024
"""The largest request body passed on: neorc's are flow files and JSON."""

CONNECT_SECONDS = 5.0
WRITE_SECONDS = 30.0

UNANSWERED = "neorc's manager does not answer: is the orchestrator running?"
TOO_LARGE = f"a request body for neorc is {MAX_BODY_BYTES} bytes at most"

HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "proxy-connection",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)

REQUEST_DROPPED = HOP_BY_HOP | {"host", "cookie", "authorization", "origin", "content-length"}
"""What the manager is not handed: this server's credentials, and what httpx sets itself."""

RESPONSE_DROPPED = HOP_BY_HOP | {"set-cookie"}

_HOST = re.compile(r"^(?:[a-z0-9.-]+|\[[0-9a-f:.]+\])(?::[0-9]{1,5})?$")
"""A ``Host`` safe to write into a policy: a name or an IP address, and a port."""

log = logging.getLogger(__name__)


def ui_policy(policy: str, origin: str | None) -> str:
    """neorc's Content-Security-Policy for its UI, as this server serves it: framed by this
    server alone, ``frame-ancestors 'self'``, and, given this server's ``origin``, with its
    ``connect-src 'self'`` narrowed to ``PREFIX`` there. Every other directive is kept as it
    is, and a policy with no ``frame-ancestors`` gets one."""
    directives: list[str] = []
    framed = False
    for directive in policy.split(";"):
        name, _, value = directive.strip().partition(" ")
        name = name.lower()
        if not name:
            continue
        if name == "frame-ancestors":
            directives.append("frame-ancestors 'self'")
            framed = True
        elif name == "connect-src" and origin is not None:
            sources = [f"{origin}{PREFIX}/" if s == "'self'" else s for s in value.split()]
            directives.append(" ".join(["connect-src", *sources]))
        else:
            directives.append(f"{name} {value.strip()}".strip())
    if not framed:
        directives.append("frame-ancestors 'self'")
    return "; ".join(directives)


def origin_of(scheme: str, host: str | None) -> str | None:
    """This server's origin as the browser named it, when the ``Host`` is plain enough to
    write into a policy; else ``None``."""
    if host is None or scheme not in ("http", "https") or not _HOST.match(host.lower()):
        return None
    return f"{scheme}://{host.lower()}"


def local_location(location: str, manager: str) -> str:
    """A redirect of the manager's, pointed back under ``PREFIX``: a path of its own, or its
    own address; any other place is left as it is."""
    parts = urlsplit(location)
    own = urlsplit(manager)
    if parts.scheme or parts.netloc:
        if (parts.scheme, parts.netloc) != (own.scheme, own.netloc):
            return location
        location = location[len(f"{own.scheme}://{own.netloc}") :] or "/"
    if location.startswith("/") and not location.startswith("//"):
        return f"{PREFIX}{location}"
    return location


def upstream_path(scope: Scope) -> str:
    """The request's path at the manager: as the client sent it, still percent-encoded,
    without ``PREFIX``, and never starting with ``//``, which would name another host."""
    raw = scope.get("raw_path") or scope["path"].encode()
    path = raw.decode("latin-1").split("?", 1)[0]
    path = path.removeprefix(PREFIX)
    return "/" + path.lstrip("/")


def forwarded(
    headers: Iterable[tuple[bytes, bytes]], dropped: frozenset[str]
) -> list[tuple[str, str]]:
    return [
        (name.decode("latin-1"), value.decode("latin-1"))
        for name, value in headers
        if name.decode("latin-1").lower() not in dropped
    ]


@dataclass(frozen=True, slots=True)
class Orchestrator:
    """The neorc manager ``robinauts start`` runs beside the server: its address, and whether
    it serves its UI, which the Flows page frames."""

    manager: str
    ui: bool = True


class NeorcProxy:
    """The ASGI app ``create_app`` mounts at ``PREFIX``: every request to the manager at
    ``manager`` (``http://127.0.0.1:<port>``), and its answer back. ``aclose`` once the server
    stops."""

    def __init__(self, manager: str, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.manager = manager.rstrip("/")
        self._http = httpx.AsyncClient(
            base_url=self.manager,
            transport=transport,
            # No read deadline: a long poll is quiet for as long as neorc holds it.
            timeout=httpx.Timeout(None, connect=CONNECT_SECONDS, write=WRITE_SECONDS),
            follow_redirects=False,
            trust_env=False,
            # Keep none of the manager's cookies, to hand back to it on someone else's behalf.
            cookies=CookieJar(DefaultCookiePolicy(allowed_domains=[])),
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return
        body = await self._body(receive)
        if body is None:
            return
        if isinstance(body, int):
            detail = {"error": "PayloadTooLarge", "detail": TOO_LARGE}
            await JSONResponse(detail, status_code=body)(scope, receive, send)
            return
        request = self._http.build_request(
            scope["method"],
            httpx.URL(path=upstream_path(scope), query=scope.get("query_string") or None),
            headers=forwarded(scope["headers"], REQUEST_DROPPED),
            content=body,
        )
        # The client going away cancels the request upstream, a long poll's included.
        relay = asyncio.ensure_future(self._relay(scope, request, send))
        gone = asyncio.ensure_future(_disconnected(receive))
        done: set[asyncio.Future[None]] = set()
        try:
            done, _ = await asyncio.wait({relay, gone}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            gone.cancel()
            if relay not in done:
                relay.cancel()
            await asyncio.gather(relay, gone, return_exceptions=True)
        if relay in done:
            relay.result()

    @staticmethod
    async def _body(receive: Receive) -> bytes | int | None:
        """The request's body; 413 past ``MAX_BODY_BYTES``; ``None`` when the client went
        away before it was sent."""
        chunks: list[bytes] = []
        size = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return None
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > MAX_BODY_BYTES:
                return 413
            chunks.append(chunk)
            if not message.get("more_body", False):
                return b"".join(chunks)

    async def _relay(self, scope: Scope, request: httpx.Request, send: Send) -> None:
        try:
            response = await self._http.send(request, stream=True)
        except httpx.HTTPError as error:
            log.warning("neorc's manager did not answer %s: %r", request.url.path, error)
            await _send_json(send, 502, {"error": "BadGateway", "detail": UNANSWERED})
            return
        try:
            await send(
                {
                    "type": "http.response.start",
                    "status": response.status_code,
                    "headers": self._headers(scope, response),
                }
            )
            async for chunk in _chunks(response):
                await send({"type": "http.response.body", "body": chunk, "more_body": True})
            await send({"type": "http.response.body", "body": b"", "more_body": False})
        finally:
            await response.aclose()

    def _headers(self, scope: Scope, response: httpx.Response) -> list[tuple[bytes, bytes]]:
        below = upstream_path(scope)
        ui = below == UI_PATH or below.startswith(f"{UI_PATH}/")
        host = next((v.decode("latin-1") for k, v in scope["headers"] if k == b"host"), None)
        origin = origin_of(scope.get("scheme", "http"), host)
        headers: list[tuple[str, str]] = []
        for name, value in response.headers.multi_items():
            lower = name.lower()
            if lower in RESPONSE_DROPPED:
                continue
            if lower == "location":
                value = local_location(value, self.manager)
            elif lower == "content-security-policy" and ui:
                value = ui_policy(value, origin)
            elif lower == "x-frame-options":
                continue
            headers.append((lower, value))
        if ui:
            headers.append(("x-frame-options", "SAMEORIGIN"))
        return [(k.encode("latin-1"), v.encode("latin-1")) for k, v in headers]


async def _chunks(response: httpx.Response) -> AsyncIterator[bytes]:
    """The body as it arrives, still encoded as the manager sent it: ``content-encoding``
    and ``content-length`` pass on unchanged."""
    async for chunk in response.aiter_raw():
        if chunk:
            yield chunk


async def _disconnected(receive: Receive) -> None:
    while True:
        message: Message = await receive()
        if message["type"] == "http.disconnect":
            return


async def _send_json(send: Send, status: int, body: dict[str, str]) -> None:
    answer = JSONResponse(body, status_code=status)
    await send({"type": "http.response.start", "status": status, "headers": answer.raw_headers})
    await send({"type": "http.response.body", "body": answer.body, "more_body": False})
