# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The ASGI application: the routes, the middleware and the error handling.

``create_api`` builds it and decides nothing else. What it serves is handed
in: the api never constructs an application service, never opens a connection
and never reads a configuration file -- the composition root does all three
and hands over the result (``docs/layout.md``, "infrastructure").

**Nothing is served without a declaration.** ``create_api`` runs
``check_declarations`` over what it built, and the lifespan runs it again when
the application starts. Those are the two moments anything is looked at, so
**routes are added between them and never after**: one added to a running
application -- a mount, a plain route, a websocket handler, a frontend -- is
served without ever having been asked what it needs. Between building and
starting, it stops the deployment instead. The test that walks the routes is
still there; this is the same walk, run where a branch nobody tested still has
to pass it.

**No ``/docs`` and no ``/redoc``.** Both load their JavaScript from a content
delivery network, and this project serves nothing from a third-party origin
(``docs/specs/frontend.md``). ``/openapi.json`` is served: it is a document,
it reaches nothing, and the interface's typed client is generated from the
committed snapshot of it (``backend/openapi.json``).

**The built interface is served here too** (``robinauts.legacy.api.ui``), under
``/ui/``, from a directory the composition root hands in. One process serves
the API and the page that calls it, which is what makes a deployment one wheel
and one PostgreSQL (``docs/specs/operations.md``).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI

from robinauts.legacy.api.access import check_declarations, public
from robinauts.legacy.api.agent_routes import agent_router
from robinauts.legacy.api.auth_routes import auth_router
from robinauts.legacy.api.conversation_routes import conversation_router
from robinauts.legacy.api.errors import install_handlers
from robinauts.legacy.api.protection import (
    SECURITY_HEADERS,
    RequestProtection,
    SecurityHeaders,
)
from robinauts.legacy.api.schemas import HealthResponse
from robinauts.legacy.api.stream_routes import stream_router
from robinauts.legacy.api.ui import add_ui
from robinauts.legacy.application import Conversations, LocalAccess, SignIn, Turns, Watch
from robinauts.legacy.domain import ConfigError

TITLE = "Robinauts"

API_VERSION = "0"
"""The version of the wire, not of the build.

The OpenAPI document is committed and a test keeps it in step, so a number
that moved with every release would make that test about releases.
"""

OPENAPI_URL = "/openapi.json"

BOTH_WAYS = (
    "an application serves a sign-in or the local development mode, never both:"
    " the mode exists because there is nothing to sign in to"
)
"""What ``create_api`` refuses to build. The composition root says it first,
with the variable to unset (``robinauts.legacy.app``); this is the door behind it, so
that no other caller -- a test, a later step -- can wire an application where
the guard would answer with a local user while sessions were being handed
out."""


Opening = Callable[[FastAPI], AbstractAsyncContextManager[None]]
"""What a lifespan is here: a context manager around the life of the process.

Narrower than Starlette's own type, which also allows one yielding a mapping
of state. Nothing here has state to hand to a request, and saying so keeps the
wrapping below honest rather than silently dropping what it was given.
"""


def create_api(
    sign_in: SignIn | None = None,
    *,
    local: LocalAccess | None = None,
    conversations: Conversations | None = None,
    turns: Turns | None = None,
    watch: Watch | None = None,
    ui_dir: Path | None = None,
    lifespan: Opening | None = None,
) -> FastAPI:
    """The application serving the api, over the services it is given.

    ``sign_in`` is the deployment's sign-in flow, or ``None`` where none is
    configured -- then ``/auth/session`` says so and the sign-in routes are
    not found. The composition root may also set it from inside ``lifespan``,
    which is where a deployment's collaborators are opened: everything here
    reads ``app.state.sign_in`` at the moment of a request rather than holding
    it, so both ways work and neither is a special case.

    ``local`` is the local development mode, the other thing a deployment may
    be (``docs/specs/sign-in.md``). It is read the same way, from
    ``app.state.local``, and asking for both is a ``ConfigError``.

    ``conversations``, ``turns`` and ``watch`` are the services the
    conversation and streaming routes call, read off ``app.state`` the same way
    and for the same reason: a deployment opens them in its lifespan, and a
    test hands in ones built over fakes. Unlike a sign-in, ``None`` is never a
    deployment's answer -- every deployment has conversations -- so a route
    that finds none says the lifespan has not run
    (``robinauts.legacy.api.access.NOT_WIRED``).

    ``ui_dir`` is the directory of built interface files this deployment
    serves under ``/ui/`` (``robinauts.legacy.api.ui``), which the composition root
    finds inside the installed package. ``None`` -- a checkout with no build,
    and every test that is about the API -- serves one page saying so, so that
    the interface is never quietly a 404.

    ``ConfigError`` -- at build time, and again at start-up -- if anything the
    application serves declares no permission, or if a router keeps routes
    somewhere ``robinauts.legacy.api.access`` does not read. Routes are added between
    those two moments; one added after start-up is never checked.
    """

    @asynccontextmanager
    async def opening(application: FastAPI) -> AsyncIterator[None]:
        check_declarations(application)
        if lifespan is None:
            yield
            return
        async with lifespan(application):
            yield

    if sign_in is not None and local is not None:
        raise ConfigError([BOTH_WAYS])

    app = FastAPI(
        title=TITLE,
        version=API_VERSION,
        summary="Conversational agents that play fair.",
        docs_url=None,
        redoc_url=None,
        openapi_url=OPENAPI_URL,
        lifespan=opening,
        # **No redirect for a trailing slash.** Starlette's default answers
        # `/api/conversations/` with a 307 to `/api/conversations`, which is a
        # surprise on an API: a generated client follows it with whatever its
        # HTTP library does to the method and the body on a redirect, and a
        # path that is not a route is better answered as what it is. Nothing
        # here is reached by a path with a slash it does not have; the step
        # that serves the built interface decides this for the static side,
        # where a directory really is asked for both ways.
        redirect_slashes=False,
    )
    app.state.sign_in = sign_in
    app.state.local = local
    app.state.conversations = conversations
    app.state.turns = turns
    app.state.watch = watch
    install_handlers(app, headers=dict(SECURITY_HEADERS))
    # The last added is the outermost, so the headers go on everything that
    # comes back -- the protection's own refusals included -- and the
    # protection is still in front of every route.
    app.add_middleware(RequestProtection)
    app.add_middleware(SecurityHeaders)
    app.include_router(auth_router)
    app.include_router(conversation_router)
    app.include_router(agent_router)
    app.include_router(stream_router)

    @app.get("/health", tags=["health"], dependencies=[public()])
    async def health() -> HealthResponse:
        """Liveness, for a load balancer or a deployment script.

        Public, and deliberately empty of everything else: it says that this
        process is answering, and nothing about the database, the providers or
        anybody signed in. A health endpoint that read the database would be a
        way to ask about the database without signing in.
        """
        return HealthResponse(status="ok")

    # Last, so that every path of the API is matched before the directory the
    # interface is served out of: a mount is a prefix, and a prefix that was
    # tried first would be answering for routes written after it.
    add_ui(app, ui_dir)
    check_declarations(app)
    return app


def openapi_document() -> dict[str, Any]:
    """The OpenAPI document of this build, as the committed snapshot holds it.

    Built from an application with nothing wired in: the document describes
    the routes, which are the same whatever a deployment is configured with.
    """
    return create_api().openapi()
