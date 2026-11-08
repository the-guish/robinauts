# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The security headers every HTTP answer carries, refusals included.

The policy is the interface's (``docs/specs/frontend.md``). An API answer carries it too: it
has no document to govern, so there it does nothing, and one list is simpler than two.
``nosniff`` keeps a browser from reading a JSON body as a script, and ``same-origin`` keeps a
path, which can name a conversation, from going to a site a person clicks through to.

``frame-src 'self'`` lets the side panel frame an MCP app's view, which this server serves under
``VIEW_HEADERS`` (``docs/adr/0007-mcp-apps-in-the-side-panel.md``).
"""

from __future__ import annotations

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

CONTENT_SECURITY_POLICY = (
    "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline';"
    " img-src 'self' data:; font-src 'self'; connect-src 'self'; frame-src 'self';"
    " base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)

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

SECURITY_HEADERS: tuple[tuple[str, str], ...] = (
    ("content-security-policy", CONTENT_SECURITY_POLICY),
    ("x-frame-options", "DENY"),
    ("x-content-type-options", "nosniff"),
    ("referrer-policy", "same-origin"),
)
"""A route may still set one of them to something else."""


class SecurityHeaders:
    """Add ``SECURITY_HEADERS`` to every HTTP answer that does not set them itself.

    Plain ASGI, so a streamed answer passes through untouched, and outermost, so the answers
    of the exception handlers carry them too.
    """

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
