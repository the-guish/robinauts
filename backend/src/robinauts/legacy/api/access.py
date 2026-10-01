# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Who is asking, and what each route needs of them.

Two things live here, and they are deliberately apart:

- **the guard**, ``current_user``: the session cookie resolved to a ``User``,
  or ``None``. It decides nothing about whether that is enough;
- **the declaration**, ``public()`` and ``signed_in()``: what a route needs.
  Every route carries one, and ``undeclared`` walks what an application
  serves and names whatever does not. It **fails closed**: a mounted
  sub-application, a plain Starlette route, a websocket handler, a frontend
  mounted with ``frontend()`` -- anything that is not an ``APIRoute`` carrying
  a declaration, or a framework route named in ``FRAMEWORK_PATHS`` by hand --
  is reported, because a check that only understands the routes it was written
  for is a check that stops working the day somebody adds another kind. The
  escape hatch is that list and nothing else: it is keyed on the name the walk
  reports, so a served directory or a ``frontend()`` at ``/ui`` is allowed by
  writing ``"/ui"`` in it -- which is how the step that serves the built
  interface will do it -- and an ``APIRoute`` is never allowed by it at all.

  A router keeps its routes in **more than one list**: ``routes``, and
  ``_low_priority_routes``, which is where ``frontend()`` puts what it serves.
  ``ROUTE_LISTS`` names them, and ``unknown_route_lists`` is the backstop
  underneath: it looks at every attribute of every router the walk touches and
  reports any **other** list holding routes, so a framework that grows a third
  one stops the deployment rather than quietly serving out of it. That is a
  question about the framework rather than about our routes, which is why it
  is asked of the object instead of guessed from a version number.

  It is not only a test. ``check_declarations`` runs both, and ``create_api``
  refuses to build or to start an application that fails either, so the door
  cannot be left open by a branch nobody ran the test on.

The POC has no roles (``docs/working-notes/poc-scope.md``, "Out"), so the two
levels of ``domain.Permission`` are the whole table: anybody, or somebody
signed in. When roles arrive, the mapping from a role to what it may do goes
in ``core`` and the declaration here grows a permission argument; what does
not change is that every route names one.

**``current_user`` is the seam for the local development mode**: a deployment
running without sign-in answers "who is this" with its one fixed local user
(``application.LocalAccess``), and nothing else about the api moves -- every
``signed_in()`` route resolves to that user, with no cookie and no 401. That
is why the question is asked in one function rather than at each route. The
two are never both set: the composition root refuses a deployment asking for
sign-in and for the local mode at once, and ``create_api`` refuses to build
one.

Request protection -- the JSON and same-origin checks on writes -- is **not**
here. It runs as middleware, before FastAPI reads anything of the request, so
that it cannot be forgotten and cannot be reached around
(``robinauts.legacy.api.protection``).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Collection, Iterator
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Request
from fastapi.routing import APIRoute
from starlette.routing import BaseRoute, Mount, Router

from robinauts.legacy.api.cookies import session_cookie
from robinauts.legacy.application import Conversations, LocalAccess, SignIn, Turns, Watch
from robinauts.legacy.domain import (
    AuthenticationError,
    ConfigError,
    Permission,
    RobinautsError,
    User,
)

PERMISSION_ATTRIBUTE = "robinauts_permission"
"""Where a declaration records the permission it asks for, for the walk to read."""

ROUTE_LISTS: tuple[str, ...] = ("routes", "_low_priority_routes")
"""Every attribute of a router that holds routes it serves.

``routes`` is the obvious one. ``_low_priority_routes`` is where FastAPI puts
what ``frontend()`` serves -- a whole directory, matched after everything else
-- and a walk that read only ``routes`` would call such an application clean
while it served every file in that directory to anybody. The names are
private ones of the framework's, which is why ``unknown_route_lists`` exists:
it is the check that does not have to know them.
"""

FRAMEWORK_PATHS: frozenset[str] = frozenset({"/openapi.json", "/ui"})
"""What may be served without declaring a permission, named one at a time.

A short, hand-written list, compared **exactly** against the name the walk
reports for a route -- the path in the message it would otherwise print, which
is ``/openapi.json`` for the document FastAPI serves, ``/ui`` for a directory
mounted there and ``/ui`` for one served there by ``frontend()``. So the
escape hatch works for all three, which is how the built interface is allowed:
``robinauts.legacy.api.ui`` mounts a directory of static files at ``/ui``, which is
not an ``APIRoute`` and has no permission to declare, and everything in it is
public -- the same bundle for every visitor, and the page it holds is where
somebody who is not signed in is sent to sign in.

It never lets an ``APIRoute`` off. A route with dependencies can declare a
permission and therefore must; this is for what has none to declare -- a
document, a directory of files -- and it is where a reviewer looks to see
what is served without one.
"""

NOT_SIGNED_IN = "this deployment needs a session: sign in at /ui/"
"""What a 401 says. Not whether the cookie was missing, unknown or expired."""

NOT_WIRED = "the deployment has not put its services on the application's state"
"""What a route finds before start-up, and it is a **programming error**.

Every deployment has conversations and runs: there is no configuration that
leaves them out, the way a deployment may have no sign-in. So a service that
is missing means the lifespan has not run -- an application being served
before it was started -- which is a mistake of ours and not something a
client can do anything about. It answers like every other 5xx of ours: the
generic body, and the whole of it in the log (``robinauts.legacy.api.errors``).
"""


def signing_in(request: Request) -> SignIn | None:
    """The deployment's sign-in, or ``None`` when none is configured.

    Set on the application's state by the composition root, which is the only
    place that builds one (``robinauts.legacy.app``).
    """
    sign_in: SignIn | None = getattr(request.app.state, "sign_in", None)
    return sign_in


def local_access(request: Request) -> LocalAccess | None:
    """The local development mode, or ``None`` when this is a deployment.

    Set on the application's state by the composition root, beside
    ``sign_in``; exactly one of the two is ever there.
    """
    local: LocalAccess | None = getattr(request.app.state, "local", None)
    return local


def conversing(request: Request) -> Conversations:
    """The deployment's conversation service; ``RobinautsError`` before start-up.

    Read off the application's state at the moment of the request, exactly as
    ``signing_in`` reads the sign-in: the composition root builds the services
    in the lifespan and puts them there, and nothing in ``api`` holds one --
    which is what lets ``create_api`` be built before a database is open and
    a test hand in a service over fakes.
    """
    return _wired(getattr(request.app.state, "conversations", None), "conversations")


def turning(request: Request) -> Turns:
    """The deployment's run service; ``RobinautsError`` before start-up.

    Read the same way, from the same state, and for the same reasons.
    """
    return _wired(getattr(request.app.state, "turns", None), "turns")


def watching(request: Request) -> Watch:
    """The deployment's watcher: a run's events, to whoever may see them.

    Read the same way, from the same state, and for the same reasons.
    """
    return _wired(getattr(request.app.state, "watch", None), "watch")


def _wired[Service](found: Service | None, name: str) -> Service:
    """``found``, or the refusal ``NOT_WIRED`` describes."""
    if found is None:
        raise RobinautsError(f"{NOT_WIRED}: app.state.{name} is None")
    return found


async def current_user(request: Request) -> User | None:
    """Whoever the session cookie stands for; ``None`` if it stands for nobody.

    An unknown, expired or absent cookie is all one answer. The difference is
    of no use to a caller and of some use to an attacker, and the application
    does not make it either (``application.SignIn.resolve_session``).

    **In the local development mode there is no cookie to read.** Sign-in is
    off, so every request is the one local user and is resolved to them --
    which is what makes the rest of the platform, ownership included, behave
    as it does in a deployment. What keeps that from being an open door is not
    here: the request never reaches a route unless it was addressed to this
    machine (``robinauts.legacy.api.protection``).
    """
    local = local_access(request)
    if local is not None:
        return await local.user()
    sign_in = signing_in(request)
    if sign_in is None:
        return None
    secret = request.cookies.get(session_cookie(secure=sign_in.config.secure))
    if not secret:
        return None
    return await sign_in.resolve_session(secret)


CurrentUser = Annotated[User | None, Depends(current_user)]
"""The guard's answer, injected. A module-level name, which FastAPI needs."""


def permissions_asked(dependant: Any) -> list[Permission]:
    """Every permission a route's dependencies declare, at any depth."""
    asked = []
    for dependency in dependant.dependencies:
        declared = getattr(dependency.call, PERMISSION_ATTRIBUTE, None)
        if declared is not None:
            asked.append(declared)
        asked.extend(permissions_asked(dependency))
    return asked


def routes_of(router: Any) -> list[Any]:
    """Every route a router serves, out of every list it keeps them in."""
    found: list[Any] = []
    for name in ROUTE_LISTS:
        found.extend(getattr(router, name, ()) or ())
    return found


def unknown_route_lists(router: Any) -> list[str]:
    """Attributes of ``router`` that hold routes and that ``ROUTE_LISTS`` misses.

    The backstop under the walk, and the only part of this that does not have
    to know what a framework calls its lists. It asks the object rather than
    the version: any attribute whose value is a list holding a ``BaseRoute``
    is somewhere routes are kept, and a name that is not in ``ROUTE_LISTS`` is
    a place nothing here has looked. Reported rather than read, because what
    is in it is unknown, and unknown is not a thing to serve.
    """
    unknown = []
    for name, value in vars(router).items():
        if name in ROUTE_LISTS or not isinstance(value, list):
            continue
        if any(isinstance(item, BaseRoute) for item in value):
            unknown.append(name)
    return sorted(unknown)


def api_routes(router: Any) -> Iterator[APIRoute]:
    """Every ``APIRoute`` the routes under ``router`` serve, wherever they are kept.

    The same walk ``_undeclared`` makes, for the callers that want the routes
    rather than a complaint about them: out of every list a router keeps them
    in (``ROUTE_LISTS``), through an **included router** -- which is where
    FastAPI keeps what ``include_router`` added rather than flattening it --
    and into a **mounted application**. One walk, so that what a check reads
    and what an answer is built from cannot come to disagree; the other caller
    is ``errors.allowed_methods``, which builds the ``Allow`` of a 405.

    Anything that is not an ``APIRoute`` and holds no routes is passed over
    here. Reporting it is ``undeclared``'s work, and is what stops a
    deployment serving it at all.
    """
    for route in routes_of(router):
        included = getattr(route, "original_router", None)
        if included is not None:
            yield from api_routes(included)
        elif isinstance(route, APIRoute):
            yield route
        elif isinstance(route, Mount):
            inside = _router_of(route.app)
            if inside is not None:
                yield from api_routes(inside)


def undeclared(app: FastAPI, *, allowed: Collection[str] = FRAMEWORK_PATHS) -> list[str]:
    """Everything ``app`` serves that declares no permission, named.

    Empty means every served route says what it needs -- and that every list
    the routers keep routes in is one this knows how to read.
    """
    return _undeclared(app.router, allowed)


def _undeclared(router: Any, allowed: Collection[str]) -> list[str]:
    """One router: the route lists it keeps, then every route in the known ones.

    It recurses into an included router and into a mounted application, and it
    refuses to be reassured by anything it does not understand: a route object
    of a kind this does not know is reported as it is, rather than skipped.
    That is the difference between a check and a habit -- a walk that looked
    only at ``APIRoute`` in ``routes`` would pass an application with a whole
    sub-application, a plain route, a websocket handler and a served directory
    bolted to it, which is exactly how such things are added.
    """
    missing = [
        f"the router keeps routes in {name}, which nothing here reads"
        for name in unknown_route_lists(router)
    ]
    for route in routes_of(router):
        included = getattr(route, "original_router", None)
        if included is not None:
            missing.extend(_undeclared(included, allowed))
            continue
        if isinstance(route, APIRoute):
            if not permissions_asked(route.dependant):
                missing.append(
                    f"{_named(route)}: declares no permission. A permission is declared on"
                    f" the route itself -- one passed to include_router is not read here"
                )
            continue
        if _where(route) in allowed:
            continue
        if isinstance(route, Mount):
            inside = _router_of(route.app)
            if inside is not None:
                missing.extend(_undeclared(inside, allowed))
            else:
                missing.append(
                    f"{_named(route)}: a mounted application whose routes cannot be read,"
                    f" so what it serves cannot be checked"
                )
            continue
        missing.append(
            f"{_named(route)}: a {type(route).__name__}, which cannot declare a permission"
        )
    return missing


def check_declarations(app: FastAPI, *, allowed: Collection[str] = FRAMEWORK_PATHS) -> None:
    """Refuse an application that serves anything without a declaration.

    ``ConfigError``, listing every one of them at once, as a start-up refusal
    does. It is asked twice: when the application is built, and again when it
    starts -- which is what catches a route added in between, and between
    those two is the only place a route may be added. **Nothing is checked
    after start-up**, because nothing looks again: a route added to a running
    application is served without ever having been asked what it needs.
    """
    missing = undeclared(app, allowed=allowed)
    if missing:
        raise ConfigError(
            f"{problem}; declare public() or signed_in(), or name it in FRAMEWORK_PATHS"
            for problem in missing
        )


def _router_of(app: Any) -> Any | None:
    """The router inside a mounted application, or ``None`` if there is none.

    A ``Router`` is mounted as itself; a whole Starlette or FastAPI
    application keeps one as ``.router``. Anything else -- a directory of
    files, a bare ASGI callable -- has no routes to read, and a mount whose
    routes cannot be read is reported rather than believed.
    """
    if isinstance(app, Router):
        return app
    inside = getattr(app, "router", None)
    return inside if isinstance(inside, Router) else None


def _where(route: Any) -> str:
    """The name the walk knows a route by, and ``FRAMEWORK_PATHS`` is keyed on.

    Its path, where it has one. Not everything served does -- what
    ``frontend()`` adds is a *group* of routes -- so a group is named by the
    paths inside it, which for a frontend at ``/ui`` is ``/ui``. Something
    with neither is named by nothing, and the caller says what type it was.
    """
    path = getattr(route, "path", None)
    if path is not None:
        return str(path)
    inside = list(getattr(route, "routes", ()) or ())
    paths = sorted({str(getattr(one, "path", "?")) for one in inside})
    return ", ".join(paths[:3]) if paths else "?"


def _named(route: Any) -> str:
    """A route as a message names it: its methods, if it has any, and ``_where``."""
    methods = getattr(route, "methods", None)
    where = _where(route)
    return f"{' '.join(sorted(methods))} {where}" if methods else where


def _declaring(permission: Permission, resolve: Callable[..., Awaitable[User | None]]) -> Any:
    """``resolve`` as a dependency that records which permission it stands for."""
    setattr(resolve, PERMISSION_ATTRIBUTE, permission)
    return Depends(resolve)


def public() -> Any:
    """Declare that this route is reached by anybody, signed in or not.

    It resolves nobody: a public route that wants to know who is there asks
    for ``CurrentUser`` as well, and one that holds no data -- ``/health`` --
    costs no lookup in the credential store.
    """

    async def permitted() -> User | None:
        return None

    return _declaring(Permission.PUBLIC, permitted)


def signed_in() -> Any:
    """Declare that this route needs a session; 401 without one.

    The value is the ``User``, so a route asking for it has the person it is
    acting for without asking twice.
    """

    async def permitted(user: CurrentUser) -> User:
        if user is None:
            raise AuthenticationError(NOT_SIGNED_IN)
        return user

    return _declaring(Permission.SIGNED_IN, permitted)


SignedIn = Annotated[User, signed_in()]
"""A route's person, and the declaration that there must be one, in one name.

``user: SignedIn`` is both halves of what a route needs: the guard resolves
the session, the declaration is what ``undeclared`` reads, and the route has
the ``User`` it acts for without asking a second time. Built once, at import,
because FastAPI reads a dependency from a module-level annotation and because
one declaration is one thing for the walk to find.
"""
