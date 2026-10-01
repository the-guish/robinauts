# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""What every write must prove before anything reads it, and what every answer carries.

There is no CSRF token (``docs/specs/sign-in.md``, "Request protection").
Three checks stand in its place, on every request whose method is not
``GET``, ``HEAD`` or ``OPTIONS``:

1. **It came from this site.** ``Sec-Fetch-Site`` is set by the browser and
   cannot be set by a page. A write may carry ``same-origin``, or ``none``
   (a navigation the person typed or bookmarked), or no such header at all --
   a browser too old to send one, which check 3 is what covers for a request
   carrying a session. **Everything else is refused**, and that is an allow
   list rather than a list of the two values that mean another site:
   ``same-site`` is a sibling subdomain, which is not us either; and a value
   nobody here has heard of, or two values a proxy folded into one header
   with a comma, is not a statement anybody can act on. Reading the known-bad
   values instead would let ``same-origin, cross-site`` through as neither.
2. **It is JSON.** A form, and the few types a page may ``fetch`` without a
   preflight, cannot be ``application/json``; asking for that type makes the
   browser ask us first, and nothing here answers a preflight. This holds for
   every write, session or none.
3. **A cookie-authenticated write names its origin.** A cookie goes with
   whatever the browser sends, so a write that carries one must come from a
   page of the public URL: ``Origin`` equal to it, or -- for a browser that
   sends no ``Origin`` -- ``Sec-Fetch-Site: same-origin``.

Before any of the three: **each of those headers may appear once**, whatever
it is spelt like. A header sent twice is answered by the first value here and
by the second somewhere else, and "somewhere else" is a proxy, a cache or a
second parser -- which is how ``Sec-Fetch-Site: same-origin`` followed by
``Sec-Fetch-Site: cross-site`` becomes an accepted cross-site write. There is
no reading of two that is safe, so two is refused.

Beside the three, and the one thing here that is **not** middleware:
``read_once``, which refuses a body that gives the same field twice, or that
is nested deeper than it can be read. It needs the body, which the framework
has already read by the time a dependency runs, and it is declared on every
route that takes one.

The names are lower-cased **here**, in one pass over the scope's own list, and
the cookies are read out of that same pass. ASGI says a server should hand
header names over in lower case and does not make it so, and the framework's
own header lookup compares what it was given: with a server that passes the
names through, ``Sec-Fetch-Site: cross-site`` beside
``sec-fetch-site: same-origin`` is two headers to us and one to it, and the
one it finds is whichever came first. Reading them once, by a name we folded
ourselves, is what makes "sent twice" mean sent twice.

**In the local development mode, two of the three change** (``docs/specs/
sign-in.md``, "Local development mode"). There is no session cookie, because
there is no sign-in: every request *is* authenticated, as the one local user,
so check 3 holds for **every** write rather than for a write carrying a
cookie, and what the ``Origin`` is compared with is the loopback origin the
request itself names. And one check is added, on every request and not only on
writes:

0. **It was addressed to this machine.** The ``Host`` header, sent once, must
   name a loopback host, and the address the server answered on must be one
   too -- a unix socket, which has no network to be reached over, counts as
   one, and a scope that says nothing about where it was answered is refused
   rather than believed. A
   page on the internet can point a host name of its own at ``127.0.0.1`` --
   the browser resolves it, connects here, and sends ``Host: evil.example``
   -- and everything a same-origin policy would have stopped is then simply
   not cross-site. The ``Host`` header is what says otherwise, and it is why
   this check covers reads: a rebound name is read from, not only written to.
   The composition root refuses to run the mode on anything but loopback
   (``domain.LocalMode``); this is the half that a server started some other
   way cannot get around.

**A refusal says a fixed sentence.** What was wrong with the request is the
request, and a body that repeated it would be reflecting whatever somebody
sent back at whoever they sent it through. The particulars -- the path, the
origin, which header -- go to the log, escaped and bounded
(``robinauts.legacy.api.logs``).

**It is middleware, not a dependency.** FastAPI reads a route's declared body
before it solves that route's dependencies, so a dependency would check the
request after the body was parsed; and a route added later could be written
without it. Here there is nothing to forget: every request to the application
passes through, whatever routes exist.

That also means it is outside the exception handlers, which live further in,
so it builds its refusal (``errors.refusal``) rather than raising it.

**What it does with a connection that is not a request.** A ``lifespan`` scope
is start-up and shutdown and passes through -- there is no request to check.
Everything else is **refused**, not passed on: the platform streams over
server-sent events (``docs/specs/wire.md``) and has no websocket route, and a
websocket is the one kind of connection that carries cookies and answers no
preflight, so a handler reachable over one would be reachable from any page on
the web. A websocket is closed with a policy-violation code before it is
accepted; a scope of a kind nothing here knows is not served at all.

``SecurityHeaders`` is the other half: what every HTTP answer carries whether
it succeeded or not. ``nosniff`` so that a JSON body is never read as a
script, and ``Referrer-Policy: same-origin`` so that a path of ours -- which
may name a conversation -- is not sent to whatever a person clicks through to.
It leaves every other kind of scope exactly as it found it.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, Request
from starlette.datastructures import MutableHeaders
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import ClientDisconnect, cookie_parser
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from robinauts.legacy.api.cookies import session_cookie
from robinauts.legacy.api.errors import refusal
from robinauts.legacy.api.logs import shown
from robinauts.legacy.api.ui import serves_files_only
from robinauts.legacy.domain import (
    CrossSiteRequestError,
    InvalidValueError,
    LocalMode,
    PayloadTooLargeError,
    RobinautsError,
    SignInConfig,
    UnsupportedMediaTypeError,
    chain,
    where,
)

_log = logging.getLogger(__name__)

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
"""Methods that change nothing, and so have nothing to prove."""

SITE_ACCEPTED = frozenset({"same-origin", "none"})
"""The only ``Sec-Fetch-Site`` values a write may carry.

An allow list, not a deny list. ``cross-site`` and ``same-site`` are the two
that plainly mean another site's page, and they are not the only two strings
that are not ``same-origin``: a proxy that folds two headers into one sends
``same-origin, cross-site``, and a browser released next year may send a word
nothing here has read about. Neither says this request came from us.
"""

JSON_MEDIA_TYPE = "application/json"

DECIDING_HEADERS = ("sec-fetch-site", "content-type", "origin")
"""The headers a write is judged by; each may be sent once or not at all."""

COOKIE_HEADER = "cookie"
"""Read in the same pass, so that a session is found by the same folded names."""

LENGTH_HEADER = "content-length"
"""How much body a write says it is sending; read in the same pass as the rest.

Not in ``DECIDING_HEADERS``, which are the three a write's *provenance* is
judged by. This one is judged for its size alone, and a write that carries two
of it is a client and a proxy disagreeing about where the body ends -- which is
refused here rather than read as either of them.
"""

MAX_BODY_BYTES = 1 * 1024 * 1024
"""How much of a request body this deployment will read. One mebibyte.

**It is not the bound on what a record may hold.** A message in the
conversation format runs to sixty-four million characters
(``domain.MAX_MESSAGE_CHARS``), because an answer a model was paid for is not
cut to fit; what a *request* may spend is another question, and the answer is
much smaller. Without this, one signed-in caller could have the deployment
buffer and parse sixty-four megabytes -- twice, since a body is read again to
check for a repeated field (``read_once``).

A megabyte is far above anything a person types and far above every body this
API takes: the largest is a turn, and a turn that needs more than this is a
file, which is a channel this version does not have
(``docs/working-notes/poc-scope.md``). It is the operator's number to raise
(``docs/specs/operations.md``); it lives here because it is the one place both
halves of reading a body pass through.
"""

MAX_LENGTH_DIGITS = len(str(MAX_BODY_BYTES))
"""How long a ``Content-Length`` this deployment reads can be, in characters.

Leading zeros off. Checked before the header is read as a number, because
CPython refuses to convert a decimal of a few thousand digits and raises a
``ValueError`` that would be a 500 over a header somebody chose the length of.
"""

TOO_LARGE_DETAIL = f"a request body is at most {MAX_BODY_BYTES} bytes"
"""What a body over the bound is told. The bound, and nothing of the request."""

_UNWINDING: tuple[type[BaseException], ...] = (ClientDisconnect, StarletteHTTPException)
"""What a body cut short is **expected** to raise on its way out.

Starlette turns a body that stops into ``ClientDisconnect``, and the framework
turns that into an ``HTTPException`` where it reads a body of its own. Both are
the machinery unwinding and are logged at DEBUG; anything else that comes out
of a request whose body was cut short is a fault of ours and is said at WARNING
rather than left to vanish behind the 413.
"""

HOST_HEADER = "host"
"""Who the request was addressed to; judged in the local development mode.

Not in ``DECIDING_HEADERS``, because those are the headers a **write** is
judged by and this one is asked of every request, reads included -- a name
pointed at this machine is read from as readily as it is written to. It may be
sent once, for the same reason the other three may.
"""

LOCAL_ORIGIN_SCHEME = "http"
"""The local development mode is served plainly: loopback needs no TLS."""

CROSS_SITE_DETAIL = "a write must come from a page of this deployment"
MEDIA_TYPE_DETAIL = f"a write must be sent as {JSON_MEDIA_TYPE}"
REPEATED_HEADER_DETAIL = "a write may carry each of the headers it is judged by only once"
LOOPBACK_DETAIL = "this server answers the loopback interface of this machine only"
"""What a refusal says. Fixed sentences: a body repeats nothing that was sent."""

WEBSOCKET_POLICY_VIOLATION = 1008
"""The close code for a connection refused on principle rather than on error."""

SECURITY_HEADERS: tuple[tuple[str, str], ...] = (
    ("x-content-type-options", "nosniff"),
    ("referrer-policy", "same-origin"),
)
"""Carried by every answer; a route may still set one of them to something else."""


@dataclass(frozen=True, slots=True)
class Refusal:
    """A request that may not be taken: what is sent back, and what is written down."""

    error: RobinautsError
    """What crosses: one of the fixed sentences above, and no request in it."""
    because: str
    """What the log says: the particulars, already escaped and bounded."""


def request_headers(scope: Scope) -> dict[str, list[str]]:
    """The headers this judges a write by, read once and folded here.

    Every value of each, in the order they arrived, under a name this
    lower-cased itself -- so that two spellings of one header are two values
    of one name rather than two headers nobody compared.
    """
    found: dict[str, list[str]] = {
        name: [] for name in (*DECIDING_HEADERS, COOKIE_HEADER, HOST_HEADER, LENGTH_HEADER)
    }
    for raw_name, raw_value in scope.get("headers", ()):
        name = raw_name.decode("latin-1").strip().lower()
        if name in found:
            found[name].append(raw_value.decode("latin-1"))
    return found


def refused(
    scope: Scope, config: SignInConfig | None, local: LocalMode | None = None
) -> Refusal | None:
    """Why this request may not be taken, or ``None`` if it may.

    ``config`` is the deployment's sign-in configuration, and ``None`` when
    none is configured: then no request carries a session of ours, and check 3
    has nothing to compare with. Checks 1 and 2 hold either way -- with nobody
    signed in there is no credential at all between another site's page and
    this API, which is the case that needs them most.

    ``local`` is the local development mode, when that is what this is. Then
    check 0 runs on every request, and check 3 runs on every write: there is
    no cookie to carry, because there is nothing to sign in to, and every
    request is the local user's. Exactly one of ``config`` and ``local`` is
    ever given.

    Cross-site is answered before the media type: a request another site's
    page sent is refused for what it is, whatever it happens to carry.

    The **interface's own paths** are past the write checks and not past check
    0: they serve files, they answer ``GET`` and ``HEAD`` and nothing else, and
    a write to one is a ``405`` with an ``Allow`` rather than a complaint about
    a body nobody would have read (``robinauts.legacy.api.ui.serves_files_only``). The
    loopback check above still runs on every request, in every mode.
    """
    method = str(scope.get("method", ""))
    # The method is compared with a fixed set below and is escaped here: it
    # comes out of the scope, which is to say off the wire, and a log line is
    # a line until something in it is a newline.
    where = f"{shown(method, most=16)} {shown(scope.get('path', ''))}"
    sent = request_headers(scope)
    if local is not None:
        elsewhere = _off_this_machine(scope, sent, local, where)
        if elsewhere is not None:
            return elsewhere
    if method in SAFE_METHODS:
        return None
    if serves_files_only(str(scope.get("path", ""))):
        # A write to the interface's own paths is a method they have not got,
        # and the router and the mount answer it with the ``405`` and the
        # ``Allow`` that say so. Refusing it here for its content type would
        # be an answer about a body that nothing was ever going to read
        # (``robinauts.legacy.api.ui.serves_files_only``).
        return None
    for name in DECIDING_HEADERS:
        if len(sent[name]) > 1:
            return Refusal(
                InvalidValueError(REPEATED_HEADER_DETAIL),
                f"{where} carries {len(sent[name])} {name} headers",
            )
    # Sent-and-empty is not the same as not sent: an empty header is a header
    # that says nothing, and a write that says nothing about where it came
    # from is refused wherever it does say something unreadable.
    site_sent = bool(sent["sec-fetch-site"])
    site = sent["sec-fetch-site"][0].strip().lower() if site_sent else ""
    if site_sent and site not in SITE_ACCEPTED:
        return Refusal(
            CrossSiteRequestError(CROSS_SITE_DETAIL),
            f"{where} carries sec-fetch-site {shown(site)}, which is not"
            f" {' or '.join(sorted(SITE_ACCEPTED))}",
        )
    media_type = ""
    if sent["content-type"]:
        media_type = sent["content-type"][0].partition(";")[0].strip().lower()
    if media_type != JSON_MEDIA_TYPE:
        return Refusal(
            UnsupportedMediaTypeError(MEDIA_TYPE_DETAIL),
            f"{where} was sent as {shown(media_type)}, not {JSON_MEDIA_TYPE}",
        )
    elsewhere = _from_another_site(sent, site, where, config, local)
    if elsewhere is not None:
        return elsewhere
    # **Last, and before a byte of the body is read.** Last because a request
    # another site's page sent is refused for **what it is**, whatever it
    # happens to carry: a cross-site write that also declared a hundred
    # megabytes is not a client of ours that sent too much. Before the body,
    # because this is what the request said it was sending -- one that says
    # nothing is counted as it is handed over (``_Bounded``).
    return _too_large(sent, where)


def _from_another_site(
    sent: dict[str, list[str]],
    site: str,
    where: str,
    config: SignInConfig | None,
    local: LocalMode | None,
) -> Refusal | None:
    """Why this credentialed write is not from a page of ours, or ``None``.

    Check 3 (this module's docstring), lifted out of ``refused`` so that the
    order of what comes after it is a line of code rather than a return inside
    three branches.
    """
    if local is not None:
        # Every request here is authenticated -- as the local user, with no
        # cookie to leave out -- so every write is judged as a credentialed
        # one. What it is compared with is the origin of the very host the
        # request named, which check 0 has already found to be loopback: a
        # person may reach the same server as ``localhost`` or as
        # ``127.0.0.1``, and both are this machine talking to itself.
        return _origin_refused(sent, site, where, sent[HOST_HEADER][0])
    if config is None or not _session_held(sent, config):
        return None
    origin = sent["origin"][0] if sent["origin"] else None
    if origin is not None:
        # ``isascii`` before ``lower``: outside ASCII, case folding is not a
        # matter of thirty-two -- U+212A, the Kelvin sign, folds onto a plain
        # ``k`` -- and an origin is written in ASCII or is not one of ours.
        named = origin.strip().removesuffix("/")
        if named.isascii() and named.lower() == config.public_url:
            return None
    elif site == "same-origin":
        return None
    return Refusal(
        CrossSiteRequestError(CROSS_SITE_DETAIL),
        f"{where} with a session named origin"
        f" {shown(origin) if origin is not None else '<none>'},"
        f" not {config.public_url}",
    )


def _too_large(sent: dict[str, list[str]], where: str) -> Refusal | None:
    """Why the body this write declares may not be read, or ``None`` if it may.

    It judges the **declaration** and not the body: the point is to answer
    before anything is read, so that a request saying it carries a hundred
    megabytes costs the deployment a refusal rather than a hundred megabytes.
    What a request really sends is counted as it is handed over
    (``_Bounded``), which is what covers a body sent without a length at all.

    Two of the header is a client and a proxy disagreeing about where the body
    ends, which is refused rather than read as either of them; one that is not
    a plain number is left alone, because a framing an ASGI server could not
    read never reaches a route.
    """
    declared = sent[LENGTH_HEADER]
    if len(declared) > 1:
        return Refusal(
            InvalidValueError(REPEATED_HEADER_DETAIL),
            f"{where} carries {len(declared)} {LENGTH_HEADER} headers",
        )
    if not declared:
        return None
    said = declared[0].strip()
    if not (said.isascii() and said.isdigit()):
        return None
    # The zeros are taken off **before** the length is looked at and before
    # anything is converted, so that a thousand of them in front of a small
    # number is neither refused as enormous nor handed to ``int``.
    counted = said.lstrip("0") or "0"
    if len(counted) > MAX_LENGTH_DIGITS or int(counted) > MAX_BODY_BYTES:
        return Refusal(
            PayloadTooLargeError(TOO_LARGE_DETAIL),
            f"{where} declared a body of {shown(said, most=32)} bytes,"
            f" over the {MAX_BODY_BYTES} this reads",
        )
    return None


def _off_this_machine(
    scope: Scope, sent: dict[str, list[str]], local: LocalMode, where: str
) -> Refusal | None:
    """Why this request was not addressed to this machine, or ``None`` if it was.

    Two questions, and both are about addresses rather than about pages. The
    ``Host`` header is the one that matters: a name somebody else controls,
    pointed at ``127.0.0.1``, is how a page on the internet reaches a server
    that only ever listened to loopback, and the ``Host`` it sends is that
    name. The address the server answered on is the second, for a process
    started some way that bound more than loopback after all -- the mode's own
    refusal (``domain.LocalMode``) is at the other end of that, and neither is
    asked to hold alone.
    """
    named = sent[HOST_HEADER]
    if len(named) != 1:
        return Refusal(
            CrossSiteRequestError(LOOPBACK_DETAIL),
            f"{where} carries {len(named)} host headers",
        )
    if not local.serves(named[0]):
        return Refusal(
            CrossSiteRequestError(LOOPBACK_DETAIL),
            f"{where} is addressed to {shown(named[0])}, which is not this machine",
        )
    wrong = _answered_elsewhere(scope, local)
    if wrong is not None:
        return Refusal(CrossSiteRequestError(LOOPBACK_DETAIL), f"{where} was answered {wrong}")
    return None


def _answered_elsewhere(scope: Scope, local: LocalMode) -> str | None:
    """How the address this was answered on fails the rule, or ``None`` if it does not.

    ASGI's ``server`` is ``(host, port)``, and ``(path, None)`` for a unix
    socket. So:

    - a **unix socket** passes. There is no interface it can be reached from,
      only a file, and whoever may open that file is already on this machine;
    - a **TCP** address must be loopback;
    - **nothing at all** is refused. The key is optional in ASGI, and a server
      that does not say where it answered is a server nothing here can check.
      This mode's whole safety is that it cannot be reached from elsewhere, so
      the unknown case is the one to say no to; every server this is run on --
      uvicorn, and ``httpx.ASGITransport`` in the tests -- sets it.
    """
    answered = scope.get("server")
    if not isinstance(answered, (list, tuple)) or len(answered) != 2:
        return "on an address it did not say, which is not one that can be checked"
    address, port = answered
    where = "" if address is None else str(address)
    # A socket path, which is what a ``None`` port means. It is judged by
    # being a path at all: a host is never written with a separator in it.
    if port is None and "/" in where:
        return None
    if local.serves(where):
        return None
    return f"on {shown(where)}, which is not loopback"


def _origin_refused(sent: dict[str, list[str]], site: str, where: str, host: str) -> Refusal | None:
    """Whether a write names the origin it was served from; ``None`` if it does.

    ``Origin`` equal to that origin, or -- for a browser that sends no
    ``Origin`` -- ``Sec-Fetch-Site: same-origin``. The same two ways a
    deployment accepts a cookie-carrying write, with the loopback origin of
    this request in place of ``public_url``.
    """
    origin = sent["origin"][0] if sent["origin"] else None
    if origin is not None:
        if _same_origin(origin, host):
            return None
    elif site == "same-origin":
        return None
    return Refusal(
        CrossSiteRequestError(CROSS_SITE_DETAIL),
        f"{where} named origin {shown(origin) if origin is not None else '<none>'},"
        f" not {LOCAL_ORIGIN_SCHEME}://{shown(host)}",
    )


def _same_origin(origin: str, host: str) -> bool:
    """Whether ``origin`` is the plain origin of ``host``, however each is spelt.

    ``isascii`` before ``lower``, as the deployment's own comparison does:
    outside ASCII, case folding is not a matter of thirty-two. The scheme must
    be ``http`` -- the mode is served plainly -- and the authority must be the
    one the request was addressed to, port and all, since a second server on
    another port of this machine is another origin.
    """
    named = origin.strip()
    if not named.isascii():
        return False
    scheme, marker, authority = named.lower().partition("://")
    if not marker or scheme != LOCAL_ORIGIN_SCHEME:
        return False
    return _authority(authority) == _authority(host)


def _authority(text: str) -> str:
    """A ``host[:port]`` in one spelling, so that two of them compare."""
    written = text.strip().rstrip("/").lower()
    return written.removesuffix(":80")


def _session_held(sent: dict[str, list[str]], config: SignInConfig) -> bool:
    """Whether the request carries this deployment's session cookie.

    Out of the same folded view as the rest: a ``Cookie:`` header found by one
    spelling and a ``cookie:`` header found by another would be a write that
    carries a session and is not checked for one.
    """
    name = session_cookie(secure=config.secure)
    return any(cookie_parser(header).get(name) for header in sent[COOKIE_HEADER])


class _Bounded:
    """A write's ``receive`` and ``send``, with the body counted as it arrives.

    **The bound in front of the framework.** A body that declares its length is
    refused before a byte of it is read (``_too_large``); one sent without a
    length declares nothing, and the framework reads and parses the whole of it
    before it solves a single dependency -- before the route's own
    ``read_once`` and before the guard that decides whether the caller is
    anybody at all. So the bytes are counted **here**, where they are handed
    over, and the chunk that would take a body past the bound is never handed
    over: what the application is given is the client having gone away, which
    every layer above unwinds cleanly and none of them invents an answer from.

    **The answer is this middleware's**, which is why ``send`` is wrapped too:
    whatever the application made of a body that stopped in the middle -- a
    400 about a body it could not parse, a 401 about a caller it never got as
    far as resolving -- is not the truth about this request, so it is dropped
    and the 413 is sent instead. Nothing has been sent while a body is being
    read, so there is always an answer left to give.

    Only for a write. A ``GET`` carries no body worth counting, and its
    ``receive`` is how a streaming response hears that the client went away,
    which is not a thing to wrap (``robinauts.legacy.api.stream_routes``).
    """

    def __init__(self, receive: Receive, send: Send) -> None:
        self._receive = receive
        self._send = send
        self._read = 0
        self.over = False
        """Whether more body arrived than this deployment reads."""
        self.answered = False
        """Whether a response has begun, and so whether there is one to give.

        A body is read before a response starts, so this is ``False`` in every
        case the bound is met in. It is here for the one that is not -- a route
        that goes on reading while it streams -- where what is already going
        out is left alone: half a response and a 413 after it would be worse
        than either.
        """
        self.unfinished = False
        """Whether the last thing sent said more of the body was coming.

        A response the application stopped in the middle of has to be closed
        by somebody (``finish``), or the connection waits for a body that is
        never coming.
        """

    async def receive(self) -> Message:
        if self.over:
            # **Without waiting.** Once the body has been cut short there is
            # nothing this connection can say that changes the answer, and a
            # caller that asks again -- a streaming response polling for a
            # disconnect, a route reading past the exception -- would otherwise
            # wait on a client that has said everything it meant to say. That
            # is a request that hangs until something else times it out.
            return {"type": "http.disconnect"}
        message = await self._receive()
        if message.get("type") != "http.request":
            return message
        self._read += len(message.get("body", b"") or b"")
        if self._read > MAX_BODY_BYTES:
            self.over = True
            return {"type": "http.disconnect"}
        return message

    async def send(self, message: Message) -> None:
        if self.over and not self.answered:
            return
        kind = message.get("type")
        if kind == "http.response.start":
            self.answered = True
        if kind == "http.response.body":
            self.unfinished = bool(message.get("more_body", False))
        await self._send(message)

    async def finish(self) -> None:
        """Close a response the application was in the middle of, if it was one.

        Only reachable where a route reads the body while it is already
        answering, which nothing here does. It is written all the same, because
        a response left with ``more_body`` set is a connection that never ends,
        and "unreachable" is a claim about today's routes.
        """
        if not self.unfinished:
            return
        self.unfinished = False
        await self._send({"type": "http.response.body", "body": b"", "more_body": False})


class RequestProtection:
    """The checks, in front of the whole application, and nothing else served.

    Pure ASGI rather than ``BaseHTTPMiddleware``: it reads the request's
    headers and cookies out of the scope, so a refused request is answered
    without a byte of its body being read. What it does to a **write**'s
    ``receive`` is count (``_Bounded``): a body that goes past what this
    deployment reads is refused as the chunks arrive, rather than after the
    framework has buffered and parsed the whole of it.

    What it needs of the deployment -- the public URL and whether the cookie
    is ``Secure``, or the local development mode and the address it is served
    on -- is read from the application's state at each request, not captured
    when the middleware is built: the composition root opens its collaborators
    in the ASGI lifespan, which runs after this object exists.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        kind = scope["type"]
        if kind == "lifespan":
            # Start-up and shutdown: no request, nothing to check.
            await self.app(scope, receive, send)
            return
        if kind == "websocket":
            await self._closed(scope, receive, send)
            return
        if kind != "http":
            _log.warning(
                "refusing an ASGI scope of type %s, which nothing here serves", shown(kind)
            )
            return
        state = scope["app"].state
        sign_in = getattr(state, "sign_in", None)
        local = getattr(state, "local", None)
        problem = refused(
            scope,
            None if sign_in is None else sign_in.config,
            None if local is None else local.mode,
        )
        if problem is None:
            if str(scope.get("method", "")) in SAFE_METHODS:
                await self.app(scope, receive, send)
                return
            bounded = _Bounded(receive, send)
            try:
                await self.app(scope, bounded.receive, bounded.send)
            except Exception as raised:
                # A route that read the body itself meets the client having
                # gone away and raises; that is this middleware's doing, and
                # the answer below is what it really was. Anything raised by a
                # request that was **not** cut short is nobody's business here.
                if not bounded.over:
                    raise
                # **Said, even so.** It is expected to be the framework
                # unwinding a body that stopped -- ``ClientDisconnect``, or an
                # ``HTTPException`` built out of it -- and that is a DEBUG for
                # whoever is looking. Anything else is a fault of ours that
                # happened to coincide with an oversized body, and it would
                # otherwise vanish behind the 413.
                _log.log(
                    logging.DEBUG if isinstance(raised, _UNWINDING) else logging.WARNING,
                    "the request whose body was cut short raised: %s | at %s",
                    chain(raised),
                    where(raised),
                )
            if bounded.over:
                _log.warning(
                    "refused: %s %s sent more than the %s bytes this reads%s",
                    shown(str(scope.get("method", "")), most=16),
                    shown(scope.get("path", "")),
                    MAX_BODY_BYTES,
                    "; its answer had already begun" if bounded.answered else "",
                )
                if bounded.answered:
                    # Half a response is going out and nothing can take it
                    # back; what is left to do is end it.
                    await bounded.finish()
                else:
                    await refusal(PayloadTooLargeError(TOO_LARGE_DETAIL))(scope, receive, send)
            return
        _log.warning("refused: %s", problem.because)
        await refusal(problem.error)(scope, receive, send)

    async def _closed(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Close a websocket without accepting it, and without reaching a route.

        The handshake is answered before anything of ours sees it: nothing
        here is meant to be reachable over a websocket, and a websocket
        carries the session cookie and is not held back by any preflight.
        """
        _log.warning("refusing a websocket connection to %s", shown(scope.get("path", "")))
        # The handshake is answered only once it has been made. A client that
        # went away first sends ``websocket.disconnect``, and closing a
        # connection that is already gone is a message a server has nowhere to
        # put -- in some of them, an error of its own.
        first = await receive()
        if first.get("type") != "websocket.connect":
            return
        await send({"type": "websocket.close", "code": WEBSOCKET_POLICY_VIOLATION})


BODY_TOO_DEEP = "body: is nested too deep"
"""What a body nothing here could finish reading is refused with.

``json`` walks a document as deep as the document is nested, and the stack it
walks it on is **shorter** by the time a dependency runs than it was when the
framework parsed the same bytes -- a route's dependencies are solved several
frames down from where the body was read. So there is a narrow band of
nesting, a few levels wide, where the framework's parse succeeds and this
one runs out of stack. What comes of that is not "the body is fine": it is a
body whose repeated fields nobody checked, and it is refused -- saying that,
and nothing about how deep it was or what was in it.
"""

BODY_TWICE = "body: a field given more than once"
"""What a body with a repeated key is refused with. Never which key it was.

A key is the request's own text (``robinauts.legacy.api.errors``), so it is not
repeated back any more than a value is.
"""


async def read_once(request: Request) -> None:
    """Refuse a JSON body that gives the same field twice, at any depth.

    ``json.loads`` keeps the **last** of a repeated key and says nothing, so
    ``{"title": "Safe", "title": "Evil"}`` is a write that asked two things
    and was answered on one of them -- and a reviewer, a log or a proxy
    reading the same bytes may well pick the other. Which one a parser takes
    is not a rule to build on: two of a field means one of them, and two that
    a tool folded together means nothing at all. The query string is held to
    the same rule (``given_once``, below).

    **A dependency, where the rest of this module is middleware.** The checks
    above are about headers and run before a byte of the body is read, which
    is what makes them impossible to forget. This one *is* about the body, and
    by the time it runs FastAPI has already read and cached the bytes
    (``Request.body``), so reading them again costs nothing and parses exactly
    what the route will be given. It is written on every route that takes a
    body, and ``test_every_route_with_a_body_reads_it_once`` is what says none
    was forgotten.

    A body that is not JSON at all is **ordinarily** not this check's to
    answer: the framework parses the bytes before it solves a dependency, so
    it has already refused one (``errors.UNREADABLE_RULES``), and the request
    protection above has already required the type. Ordinarily, because this
    parse runs further down the stack than that one: a body nested within a
    few levels of the interpreter's limit gets through there and runs out of
    stack here. That is ``BODY_TOO_DEEP``, and it is a refusal rather than a
    shrug, because a body this could not read is a body whose repeated fields
    nobody checked.

    **The bound on how large a body may be is in front of this, twice.**
    ``MAX_BODY_BYTES`` is checked on the declared length before a byte is read
    (``_too_large``) and on the chunks as they are handed over (``_Bounded``),
    which is what covers a body sent with no length at all; what is left here
    is a backstop. This module's earlier note asked for exactly that, because
    this reads the body a second time and the work a request can ask for was
    otherwise unbounded. The bound is the **wire's** and not the format's: a
    stored message may hold sixty-four million characters
    (``domain.MAX_MESSAGE_CHARS``) and no request may carry one.
    """
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        # **The backstop.** Nothing should reach here over the bound: a
        # declared length is refused before a byte is read, and what arrives
        # without one is counted as it is handed over (``_Bounded``). It is
        # kept because this is the last place the whole body is in hand, and
        # because a route reached some way that did not come through the
        # middleware would otherwise have no bound at all.
        raise PayloadTooLargeError(TOO_LARGE_DETAIL)
    if not body:
        return
    try:
        json.loads(body, object_pairs_hook=_one_of_each)
    except json.JSONDecodeError:
        # Not readable JSON, which is somebody else's refusal: see above.
        # Caught by its own class and **not** as a ``ValueError``, which
        # ``InvalidValueError`` also is: the refusal below would be swallowed
        # by the wider one, and a body that gave a field twice would go
        # through as whatever the last of it said.
        return
    except RecursionError:
        # Deeper than the stack left here, and the message says only that.
        # Unhandled it would be a 500 with a traceback in the log, for a
        # request that is nobody's mistake but its sender's.
        raise InvalidValueError(BODY_TOO_DEEP) from None


StrictJson = Annotated[None, Depends(read_once)]
"""The check above, as a route declares it: ``read_once: StrictJson``."""


def given_once(request: Request, *names: str) -> None:
    """``InvalidValueError`` if any of those query parameters was sent twice.

    FastAPI reads the **last** of a repeated query parameter and says nothing,
    so ``?limit=1000&limit=2`` is a request that got two answers to one
    question and was told about neither. Which one a framework picks is not
    something to build a bound on: a client that sent two means one of them,
    and a proxy that folded two together means nothing at all. So it is
    refused, naming the parameter and not what was in it.

    Here beside ``read_once`` because they are one rule asked of the two halves
    of a request: a query string that says a thing twice and a body that does.
    It is a call rather than a dependency, since which parameters a route takes
    is the route's to say.
    """
    given = [name for name, _ in request.query_params.multi_items()]
    for name in names:
        if given.count(name) > 1:
            raise InvalidValueError(f"query.{name}: given more than once")


def _one_of_each(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    """``pairs`` as an object; ``InvalidValueError`` if a key is in it twice.

    ``json.loads`` calls this for **every** object of the document, so a
    repeated key anywhere in it is refused, and not only at the top.
    """
    seen: set[str] = set()
    for key, _ in pairs:
        if key in seen:
            raise InvalidValueError(BODY_TWICE)
        seen.add(key)
    return dict(pairs)


class SecurityHeaders:
    """Add ``SECURITY_HEADERS`` to every HTTP answer that does not set them itself."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in SECURITY_HEADERS:
                    headers.setdefault(name, value)
            await send(message)

        await self.app(scope, receive, with_headers)
