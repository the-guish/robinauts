# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The security headers every HTTP answer carries, refusals included.

The policy is the interface's (``docs/specs/frontend.md``). An API answer carries it too: it
has no document to govern, so there it does nothing, and one list is simpler than two.
``nosniff`` keeps a browser from reading a JSON body as a script, and ``same-origin`` keeps a
path, which can name a conversation, from going to a site a person clicks through to.

``frame-src 'self'`` lets the side panel frame an MCP app's view, which this server serves under
``VIEW_HEADERS`` (``docs/adr/0007-mcp-apps-in-the-side-panel.md``). With the orchestrator on, the
Flows page frames neorc's UI too, on an origin of its own (``docs/architecture/orchestration.md``):
that origin, and nothing else, is added to ``frame-src``. Nothing else of the policy moves.
"""

from __future__ import annotations

import re

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

ORIGIN = re.compile(r"^https?://(?:[a-z0-9.-]+|\[[0-9a-f:.]+\])(?::[0-9]{1,5})?$")
"""An origin as a policy names it: a scheme, a host and a port, and nothing else. A path, a
query, a wildcard, a quote, a space or a ``;`` would each change what the policy says."""


def content_security_policy(frame_src: tuple[str, ...] = ()) -> str:
    """The interface's policy, framing this server's own pages and the origins in
    ``frame_src``. ``ValueError`` for anything in it that is not an origin."""
    for origin in frame_src:
        if not ORIGIN.fullmatch(origin):
            raise ValueError(f"not an origin to frame: {origin!r}")
    framed = " ".join(("'self'", *frame_src))
    return (
        "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline';"
        f" img-src 'self' data:; font-src 'self'; connect-src 'self'; frame-src {framed};"
        " base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    )


CONTENT_SECURITY_POLICY = content_security_policy()
"""The policy of a server that frames nothing but its own pages."""

VIEW_CONTENT_SECURITY_POLICY = (
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline';"
    " img-src data:; font-src data:; connect-src 'none'; base-uri 'none';"
    " form-action 'none'; frame-ancestors 'self'; sandbox allow-scripts"
)
"""An MCP app's view: a tool server's HTML, with its script and style inline and nothing to
fetch, framed by the interface alone. It reaches nothing but its frame's parent, by
``postMessage``. ``sandbox`` gives it an origin of its own wherever it is opened, even as a page
of its own, so it never acts as this server's; whatever origins the server asks for are not
given."""

VIEW_HEADERS: tuple[tuple[str, str], ...] = (
    ("content-security-policy", VIEW_CONTENT_SECURITY_POLICY),
    ("x-frame-options", "SAMEORIGIN"),
    ("cache-control", "no-store"),
)
"""The headers of an MCP app's view, in the place of the interface's."""


def security_headers(frame_src: tuple[str, ...] = ()) -> tuple[tuple[str, str], ...]:
    """The headers every answer carries, with ``frame_src`` framed beside this server's own
    pages. A route may still set one of them to something else."""
    return (
        ("content-security-policy", content_security_policy(frame_src)),
        ("x-frame-options", "DENY"),
        ("x-content-type-options", "nosniff"),
        ("referrer-policy", "same-origin"),
    )


SECURITY_HEADERS = security_headers()
"""The headers of a server that frames nothing but its own pages."""


class SecurityHeaders:
    """Add ``headers``, ``SECURITY_HEADERS`` unless told otherwise, to every HTTP answer that
    does not set them itself.

    Plain ASGI, so a streamed answer passes through untouched, and outermost, so the answers
    of the exception handlers carry them too.
    """

    def __init__(
        self, app: ASGIApp, headers: tuple[tuple[str, str], ...] = SECURITY_HEADERS
    ) -> None:
        self.app = app
        self.headers = headers

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in self.headers:
                    headers.setdefault(name, value)
            await send(message)

        await self.app(scope, receive, with_headers)
