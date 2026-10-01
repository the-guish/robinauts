# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Who is asking, what each route needs, and that nothing is served without saying.

The last one is the point of this module. A route that declares no permission
is a route nobody guarded, and that does not happen through a bad decision but
through a forgotten line in a file written six months from now -- or through a
kind of route the check was never written for. So the walk
(``robinauts.legacy.api.undeclared``) fails **closed**: it recurses into included
routers and mounted applications, and it names anything it does not recognise
as a declared ``APIRoute`` or as a framework route allowed by hand.

It is shown to bite on every way something can be served: an ``APIRoute`` with
no declaration, a mounted sub-application, a mounted directory, a plain
Starlette route, a websocket handler, and a frontend added with ``frontend()``
-- which a router keeps in a **second** list, not in ``routes``.

The one thing that **is** allowed is the interface: a directory of built files
mounted at ``/ui`` (``robinauts.legacy.api.ui``), named in ``FRAMEWORK_PATHS`` with
``/openapi.json``. So the fixtures here put their extra directories somewhere
else, ``/pages``, where the escape hatch does not reach them.

Under all of that is the backstop, and the backstop is what this file really
guards: ``unknown_route_lists`` asks each router which of its attributes hold
routes, and the walk refuses any list it was not written to read. That check
is about the framework rather than about our routes, which is why it asks the
object rather than a version number -- and why a FastAPI that grows a third
list fails here instead of quietly serving out of it.

And it is not only a test: the same walk runs when the application is built
and again when it starts, so a branch nobody ran the tests on still cannot
leave a door open.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import Annotated, Any

import fastapi
import httpx
import pytest
from fastapi import APIRouter, FastAPI, WebSocket
from fastapi.routing import APIRoute
from starlette.routing import BaseRoute
from starlette.staticfiles import StaticFiles

from aio import asyncio_test
from robinauts.legacy.api import (
    FRAMEWORK_PATHS,
    NOT_SIGNED_IN,
    ROUTE_LISTS,
    check_declarations,
    create_api,
    permissions_asked,
    public,
    routes_of,
    session_cookie,
    signed_in,
    undeclared,
    unknown_route_lists,
)
from robinauts.legacy.domain import ConfigError, Permission, User
from webapp import (
    LOOPBACK_URL,
    PUBLIC_URL,
    Wired,
    cookie,
    open_session,
    running,
    serving,
    wired,
)

DECLARED = {
    ("GET", "/auth/session"): Permission.PUBLIC,
    ("GET", "/auth/login/{provider}"): Permission.PUBLIC,
    ("GET", "/auth/callback/{provider}"): Permission.PUBLIC,
    ("POST", "/auth/logout"): Permission.PUBLIC,
    ("GET", "/health"): Permission.PUBLIC,
    ("GET", "/"): Permission.PUBLIC,
    ("HEAD", "/"): Permission.PUBLIC,
    ("GET", "/ui"): Permission.PUBLIC,
    ("HEAD", "/ui"): Permission.PUBLIC,
    ("GET", "/api/conversations"): Permission.SIGNED_IN,
    ("GET", "/api/conversations/{conversation_id}"): Permission.SIGNED_IN,
    ("PATCH", "/api/conversations/{conversation_id}"): Permission.SIGNED_IN,
    ("DELETE", "/api/conversations/{conversation_id}"): Permission.SIGNED_IN,
    ("PUT", "/api/conversations/{conversation_id}/model"): Permission.SIGNED_IN,
    ("POST", "/api/conversations/{conversation_id}/runs/{run_id}/cancel"): Permission.SIGNED_IN,
    ("GET", "/api/agents"): Permission.SIGNED_IN,
    ("GET", "/api/models"): Permission.SIGNED_IN,
    ("POST", "/api/turns"): Permission.SIGNED_IN,
    ("POST", "/api/conversations/{conversation_id}/turns"): Permission.SIGNED_IN,
    ("GET", "/api/runs/{run_id}/events"): Permission.SIGNED_IN,
}
"""Every route of this build, and the permission it asks for.

Written out rather than derived: a route whose declaration changes, or a route
added at all, is then a change to this file and something a reviewer reads.

``/`` and ``/ui`` are the two navigations that land in the interface
(``robinauts.legacy.api.ui``), each answering ``GET`` and ``HEAD``; the files
themselves are the mount below, which is not an ``APIRoute`` and is allowed by
name.
"""

SOMEWHERE_ELSE = "/pages"
"""Where the fixtures below put a directory, so that it is not the real one."""


def declarations(
    routes: Iterable[Any],
) -> Iterator[tuple[tuple[str, str], list[Permission]]]:
    """What each ``APIRoute`` declares, for the table above.

    The *fail-closed* question -- is anything served that this does not
    understand -- is ``undeclared``'s, and is asked separately below. This one
    only reads the declarations of the routes there are.
    """
    for route in routes:
        included = getattr(route, "original_router", None)
        if included is not None:
            yield from declarations(included.routes)
        elif isinstance(route, APIRoute):
            for method in sorted(route.methods or ()):
                yield (method, route.path), permissions_asked(route.dependant)


def test_every_route_declares_exactly_the_permission_it_needs() -> None:
    asked = dict(declarations(create_api().routes))

    assert asked == {where: [permission] for where, permission in DECLARED.items()}


def test_nothing_this_build_serves_is_undeclared() -> None:
    assert undeclared(create_api()) == []
    assert FRAMEWORK_PATHS == frozenset({"/openapi.json", "/ui"})


def with_an_undeclared_route() -> FastAPI:
    app = create_api()

    @app.get("/forgotten")
    async def forgotten() -> dict[str, str]:  # pragma: no cover -- never requested
        return {}

    return app


def with_a_mounted_application() -> FastAPI:
    """A whole FastAPI underneath a path: its routes are served, and are inside it."""
    app = create_api()
    sub = FastAPI()

    @sub.get("/secret")
    async def secret() -> dict[str, str]:  # pragma: no cover -- never requested
        return {"everybody": "sees this"}

    app.mount("/sub", sub)
    return app


def with_a_frontend(tmp: Any) -> FastAPI:
    """A whole directory served under a path -- which is not in ``routes``.

    ``frontend()`` puts what it serves in a list of its own, so a walk that
    read ``routes`` alone would call this application clean while it handed
    out every file in that directory to anybody who asked.

    At ``/pages`` and not at ``/ui``: ``/ui`` is where this build really serves
    the interface and is named in ``FRAMEWORK_PATHS``, so a second directory
    there would be testing the escape hatch instead of the walk.
    """
    app = create_api()
    app.frontend(SOMEWHERE_ELSE, directory=str(tmp))
    return app


def with_a_frontend_in_a_router(tmp: Any) -> FastAPI:
    """The same, one level down: a router's own second list, through an include."""
    app = create_api()
    router = APIRouter()
    router.frontend(SOMEWHERE_ELSE, directory=str(tmp))
    app.include_router(router)
    return app


def with_a_declaration_on_the_include(tmp: Any = None) -> FastAPI:
    """A permission passed to ``include_router`` rather than written on the route.

    It does guard the route at run time, and it is still refused: the walk
    reads what is on the route, a reviewer reads what is on the route, and a
    declaration that is somewhere else is a declaration that moves when
    somebody rearranges an include.
    """
    app = create_api()
    router = APIRouter()

    @router.get("/carried")
    async def carried() -> dict[str, str]:  # pragma: no cover -- never requested
        return {}

    app.include_router(router, dependencies=[public()])
    return app


def with_a_mounted_directory(tmp: Any) -> FastAPI:
    """An ASGI application that is not a router at all: nothing to look inside."""
    app = create_api()
    app.mount("/files", StaticFiles(directory=str(tmp)))
    return app


def with_a_plain_route() -> FastAPI:
    """``add_route`` makes a Starlette ``Route``, which has no dependencies at all."""
    app = create_api()

    async def plain(request: Any) -> Any:  # pragma: no cover -- never requested
        raise AssertionError("never called")

    app.add_route("/plain", plain, methods=["GET"])
    return app


def with_a_websocket() -> FastAPI:
    app = create_api()

    @app.websocket("/ws")
    async def listen(websocket: WebSocket) -> None:  # pragma: no cover -- never opened
        await websocket.accept()

    return app


def with_an_undeclared_route_in_a_router() -> FastAPI:
    """Included routers are nested, so the walk has to go into them."""
    app = create_api()
    router = APIRouter(prefix="/inner")

    @router.get("/thing")
    async def thing() -> dict[str, str]:  # pragma: no cover -- never requested
        return {}

    app.include_router(router)
    return app


def test_the_walk_bites_on_every_kind_of_route_that_is_served(tmp_path: Any) -> None:
    """Every real way to serve something from a FastAPI application."""
    (tmp_path / "index.html").write_text("<!doctype html><title>ui</title>")
    broken = {
        "/forgotten": with_an_undeclared_route(),
        "/sub/secret": with_a_mounted_application(),
        "/files": with_a_mounted_directory(tmp_path),
        "/plain": with_a_plain_route(),
        "/ws": with_a_websocket(),
        "/inner/thing": with_an_undeclared_route_in_a_router(),
        "/pages": with_a_frontend(tmp_path),
        "/pages in a router": with_a_frontend_in_a_router(tmp_path),
        "/carried": with_a_declaration_on_the_include(),
    }

    named = {where: undeclared(app) for where, app in broken.items()}

    assert all(problems for problems in named.values()), named
    assert "declares no permission" in " ".join(named["/forgotten"])
    assert "declares no permission" in " ".join(named["/sub/secret"])
    assert "declares no permission" in " ".join(named["/inner/thing"])
    assert "routes cannot be read" in " ".join(named["/files"])
    assert "cannot declare a permission" in " ".join(named["/plain"])
    assert "cannot declare a permission" in " ".join(named["/ws"])
    assert "_FrontendRouteGroup" in " ".join(named["/pages"])
    assert "_FrontendRouteGroup" in " ".join(named["/pages in a router"])
    # A declaration on the include is not one on the route, and says so.
    assert "on the route itself" in " ".join(named["/carried"])


@asyncio_test
async def test_a_frontend_added_to_this_build_stops_it_from_starting(
    tmp_path: Any,
) -> None:
    """A second directory of files does not arrive by accident either."""
    (tmp_path / "index.html").write_text("<!doctype html><title>ui</title>")
    app = with_a_frontend(tmp_path)

    with pytest.raises(ConfigError):
        async with running(app):  # pragma: no cover -- start-up fails
            pass


def test_building_an_application_that_serves_something_undeclared_is_refused() -> None:
    """The runtime check, not only the test: it runs inside ``create_api``."""
    app = create_api()
    sub = FastAPI()

    @sub.get("/secret")
    async def secret() -> dict[str, str]:  # pragma: no cover -- never requested
        return {}

    app.mount("/sub", sub)

    with pytest.raises(ConfigError) as raised:
        check_declarations(app)

    assert "declares no permission" in "\n".join(raised.value.problems)
    assert "FRAMEWORK_PATHS" in "\n".join(raised.value.problems)


@asyncio_test
async def test_an_application_with_an_undeclared_route_does_not_start() -> None:
    """A route added after the application was built is caught at start-up."""
    app = with_an_undeclared_route()

    with pytest.raises(ConfigError):
        async with running(app):  # pragma: no cover -- start-up fails
            pass


@asyncio_test
async def test_the_application_this_build_makes_does_start() -> None:
    app = create_api(wired().sign_in)

    async with running(app):
        pass


def test_a_framework_route_passes_only_by_being_named() -> None:
    """The allow list is compared exactly, and is the only way past the walk."""
    app = create_api()

    assert undeclared(app) == []
    assert undeclared(app, allowed=()) == [
        "GET HEAD /openapi.json: a Route, which cannot declare a permission",
        "/ui: a mounted application whose routes cannot be read, so what it serves"
        " cannot be checked",
    ]


@pytest.mark.parametrize("serving_the_ui", ["frontend", "mount"])
def test_the_escape_hatch_allows_a_served_directory_by_name(
    tmp_path: Any, serving_the_ui: str
) -> None:
    """How a later step will serve the built interface: one name in the table.

    Both ways of putting a directory under a path are reported by the same
    name -- the one in the message -- so both are allowed by writing that
    name, and neither is allowed by anything else.
    """
    (tmp_path / "index.html").write_text("<!doctype html><title>ui</title>")
    app = create_api()
    if serving_the_ui == "frontend":
        app.frontend(SOMEWHERE_ELSE, directory=str(tmp_path))
    else:
        app.mount(SOMEWHERE_ELSE, StaticFiles(directory=str(tmp_path)))

    # Named: it passes, and the walk is otherwise unchanged.
    named = {*FRAMEWORK_PATHS, SOMEWHERE_ELSE}
    check_declarations(app, allowed=named)
    assert undeclared(app, allowed=named) == []

    # Not named: it is still refused, and the message holds the very name
    # that would have allowed it.
    with pytest.raises(ConfigError) as raised:
        check_declarations(app)
    assert SOMEWHERE_ELSE in "\n".join(raised.value.problems)
    # And the name does not allow the framework's own route by accident.
    assert undeclared(app, allowed={SOMEWHERE_ELSE, "/ui"}) == [
        "GET HEAD /openapi.json: a Route, which cannot declare a permission"
    ]


def test_the_escape_hatch_never_lets_an_api_route_off(tmp_path: Any) -> None:
    """A route with dependencies can declare a permission, so it must.

    The table is for what has nothing to declare with -- a document, a
    directory of files -- and naming a path does not turn the check off for a
    route that could have said what it needs.
    """
    app = with_an_undeclared_route()

    assert undeclared(app, allowed={*FRAMEWORK_PATHS, "/forgotten"}) == [
        "GET /forgotten: declares no permission. A permission is declared on the route"
        " itself -- one passed to include_router is not read here"
    ]


READ_FASTAPI = ("0.141.",)
"""The FastAPI releases whose router internals somebody has actually read.

``ROUTE_LISTS`` names private attributes. The test below is the reminder that
a version outside this list is a version whose ``APIRouter`` has to be opened
and looked at again -- and the backstop beside it is what makes the answer
"the walk already refuses what it cannot read" rather than "we hope so".
"""


def test_the_walk_knows_every_route_list_fastapi_keeps(tmp_path: Any) -> None:
    """Every list a router really keeps routes in is one ``ROUTE_LISTS`` names.

    Asked of an application carrying one of everything, so that each kind has
    been put wherever the framework puts it: an ``APIRoute``, an included
    router, a mount, a websocket, a plain route and a frontend.
    """
    (tmp_path / "index.html").write_text("<!doctype html><title>ui</title>")
    app = create_api()
    router = APIRouter()

    @router.get("/thing", dependencies=[public()])
    async def thing() -> dict[str, str]:  # pragma: no cover -- never requested
        return {}

    @app.websocket("/ws")
    async def listen(websocket: WebSocket) -> None:  # pragma: no cover -- never opened
        await websocket.accept()

    app.include_router(router)
    app.mount("/sub", FastAPI())
    app.add_route("/plain", lambda request: None, methods=["GET"])
    app.frontend(SOMEWHERE_ELSE, directory=str(tmp_path))

    routers = [app.router, router]
    kept = {
        name
        for one in routers
        for name, value in vars(one).items()
        if isinstance(value, list) and any(isinstance(item, BaseRoute) for item in value)
    }

    assert kept <= set(ROUTE_LISTS), (
        f"FastAPI {fastapi.__version__} keeps routes in {sorted(kept - set(ROUTE_LISTS))},"
        f" which robinauts.legacy.api.access.ROUTE_LISTS does not name"
    )
    assert all(unknown_route_lists(one) == [] for one in routers)
    assert fastapi.__version__.startswith(READ_FASTAPI), (
        f"FastAPI {fastapi.__version__} is outside the releases whose APIRouter has been"
        f" read ({', '.join(READ_FASTAPI)}x): open fastapi.routing.APIRouter, check which"
        f" attributes hold routes, update ROUTE_LISTS and READ_FASTAPI"
    )
    # And the walk really reached all of it: every kind is reported.
    named = " ".join(undeclared(app))
    for kind in ("APIWebSocketRoute", "_FrontendRouteGroup", "/plain", "/docs"):
        assert kind in named, named


def test_the_backstop_names_a_route_list_nothing_reads() -> None:
    """A framework that grows a list of its own is refused, not walked around.

    This is the half of the check that does not depend on knowing what those
    lists are called: a list of routes under a name nobody reads is a place
    requests are served from and nothing has looked.
    """
    app = create_api()
    app.router._invented_routes = list(app.router.routes)  # type: ignore[attr-defined]

    assert unknown_route_lists(app.router) == ["_invented_routes"]
    assert "the router keeps routes in _invented_routes" in " ".join(undeclared(app))

    with pytest.raises(ConfigError):
        check_declarations(app)


def test_routes_of_reads_every_list_the_walk_knows(tmp_path: Any) -> None:
    (tmp_path / "index.html").write_text("<!doctype html><title>ui</title>")
    app = with_a_frontend(tmp_path)

    found = routes_of(app.router)

    assert len(found) == len(app.router.routes) + len(app.router._low_priority_routes)


def guarded_app(deployment: Wired) -> FastAPI:
    """An application with the one kind of route this build does not have yet.

    ``signed_in()`` has no route of its own until the conversation routes
    arrive, and a declaration nothing declares is a declaration nothing tests.
    """
    app = create_api(deployment.sign_in)

    @app.get("/only-mine")
    async def only_mine(user: Annotated[User, signed_in()]) -> dict[str, str]:
        return {"user": str(user.id)}

    @app.get("/anybody", dependencies=[public()])
    async def anybody() -> dict[str, str]:
        return {"seen": "anybody"}

    return app


def browser(app: FastAPI, *, base_url: str = PUBLIC_URL) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=base_url,
        follow_redirects=False,
    )


@asyncio_test
async def test_a_signed_in_route_refuses_a_request_with_no_session() -> None:
    async with browser(guarded_app(wired())) as client:
        response = await client.get("/only-mine")

    assert response.status_code == 401
    assert response.json() == {"error": "AuthenticationError", "detail": NOT_SIGNED_IN}
    # No WWW-Authenticate: the browser must not open a dialogue of its own,
    # since signing in here is a page of ours.
    assert "www-authenticate" not in response.headers


@pytest.mark.parametrize("held", ["", "not-a-secret", "x" * 43])
@asyncio_test
async def test_a_cookie_that_names_no_session_is_nobody(held: str) -> None:
    """Missing, misshapen and unknown are one answer: not signed in."""
    deployment = wired()
    await open_session(deployment)

    async with browser(guarded_app(deployment)) as client:
        response = await client.get("/only-mine", headers=cookie(session_cookie(secure=True), held))

    assert response.status_code == 401


@asyncio_test
async def test_a_signed_in_route_answers_the_person_the_cookie_names() -> None:
    deployment = wired()
    secret, user = await open_session(deployment)

    async with browser(guarded_app(deployment)) as client:
        response = await client.get(
            "/only-mine", headers=cookie(session_cookie(secure=True), secret)
        )

    assert response.status_code == 200
    assert response.json() == {"user": str(user.id)}


@asyncio_test
async def test_a_session_that_has_expired_is_nobody() -> None:
    deployment = wired()
    secret, _ = await open_session(deployment)
    deployment.clock.advance(deployment.config.session_life)

    async with browser(guarded_app(deployment)) as client:
        response = await client.get(
            "/only-mine", headers=cookie(session_cookie(secure=True), secret)
        )

    assert response.status_code == 401


@asyncio_test
async def test_a_public_route_is_reached_without_a_session() -> None:
    async with browser(guarded_app(wired())) as client:
        response = await client.get("/anybody")

    assert (response.status_code, response.json()) == (200, {"seen": "anybody"})


@pytest.mark.parametrize(
    ("public_url", "expected"),
    [(PUBLIC_URL, "__Host-robinauts_session"), (LOOPBACK_URL, "robinauts_session")],
)
@asyncio_test
async def test_the_guard_reads_the_cookie_this_deployment_spells(
    public_url: str, expected: str
) -> None:
    """The other deployment's spelling of the cookie names no session here."""
    deployment = wired(public_url=public_url)
    secret, user = await open_session(deployment)
    other = "robinauts_session" if expected.startswith("__Host-") else "__Host-robinauts_session"

    async with serving(deployment.sign_in, base_url=public_url) as client:
        under_its_name = await client.get("/auth/session", headers=cookie(expected, secret))
        under_the_other = await client.get("/auth/session", headers=cookie(other, secret))

    assert session_cookie(secure=public_url.startswith("https://")) == expected
    assert under_its_name.json()["user"]["id"] == str(user.id)
    assert under_the_other.json()["user"] is None
