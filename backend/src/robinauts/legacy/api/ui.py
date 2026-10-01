# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The built interface, served out of the wheel.

One process serves the API and the page that calls it (``docs/specs/backend.md``,
"Web"): there is no second web server, no CDN and no third-party origin, so an
air-gapped install works (``docs/specs/frontend.md``).

**Where it is.** The files are ``robinauts/ui/`` inside the wheel, put there at
build time from ``frontend/dist`` (``backend/hatch_build.py``), and the
composition root hands the directory in -- ``create_api(ui_dir=...)`` -- because
``api`` reads no configuration and finds nothing for itself
(``docs/layout.md``). A checkout with no build has no such directory, and then
every path under ``/ui/`` answers one page saying so rather than a 404 nobody
can read.

**Three paths, and why each is what it is.**

- ``/ui/`` is the page. The built ``index.html`` names its assets
  **relatively** (``base: "./"`` in ``frontend/vite.config.ts``, so the same
  build works under any prefix), which is why this and not ``/ui`` is where the
  page is served: a browser resolves ``./assets/x.js`` against the directory of
  the document it came from, and a document at ``/ui`` has ``/`` for its
  directory.
- ``/ui`` is therefore a **redirect** to ``/ui/`` and not a copy of the page.
  ``redirect_slashes`` is off for the whole application (``robinauts.legacy.api.web``),
  so nothing does this for us and the route is written out. It is what
  Starlette's own directory handling would have answered, and what every link
  saying "/ui" needs.
- ``/`` is a redirect to ``/ui/`` as well: a deployment is served at the root of
  an origin (``docs/specs/operations.md``), and somebody who types the host name
  is asking for the interface.

Both redirects are plain navigations, so both are ``public()`` and both are out
of the OpenAPI document: it describes the API a client is generated from, and a
browser needs no schema to follow a ``Location``.

**Everything under ``/ui/`` is public.** The page itself decides what to show
somebody who is not signed in -- it asks ``GET /auth/session`` and draws the
sign-in page (``docs/specs/sign-in.md``) -- and a login screen that could only
be fetched by somebody already signed in would be a circle. Nothing under
``/ui/`` is anybody's data: it is the same bundle for every visitor.

**The headers.** ``UI_HEADERS`` carries the Content-Security-Policy
``docs/specs/frontend.md`` specifies, on the interface's own answers and on
nothing else: the API's answers are JSON read by a script and carry the
policy-free ``nosniff`` and ``Referrer-Policy`` every answer gets
(``robinauts.legacy.api.protection.SECURITY_HEADERS``). Adding ``default-src 'none'``
to a JSON body would say nothing and would have to be kept in step with a
document that has no scripts to govern.

Caching follows the file names: the assets are content-hashed by the build, so
they are immutable for a year, and ``index.html`` -- which is what names this
build's hashes -- is ``no-store``, so a deployment that is upgraded is not
answered out of a cache that still names the files of the one before it.
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import FastAPI, Request
from starlette.datastructures import MutableHeaders
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import RedirectResponse, Response
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from robinauts.legacy.api.access import public
from robinauts.legacy.api.errors import http_refusal

UI_PATH = "/ui/"
"""Where the interface is served; a sign-in ends by landing in it."""

UI_MOUNT = "/ui"
"""The same place without its trailing slash: the mount, and the redirect."""

UI_DIRECTORY = "ui"
"""The directory inside the installed package that holds the built files.

Written down here because three things have to agree about it: the build hook
that fills it (``backend/hatch_build.py``), the composition root that finds it
(``robinauts.legacy.app.packaged_ui``) and the check that looks inside a built wheel
(``scripts/check-wheel.sh``).
"""

INDEX = "index.html"
"""The one page. Everything else the interface shows is a hash route."""

ASSETS = "assets"
"""Where the build puts the files whose names carry a hash of their contents."""

HASH_CHARS = 8
"""How many characters the digest in a built file name has. Vite's default."""

HASHED = re.compile(rf"-[A-Za-z0-9_-]{{{HASH_CHARS}}}\.[A-Za-z0-9]+$")
"""A file name Vite built from the contents of the file.

``index-D93cLA-w.js``: the name, a hyphen, the eight base64url characters Vite
appends, the extension. It is what makes a year of ``immutable`` safe -- a file
that changes gets another name -- so it is **matched**, not assumed of
everything under ``assets/``: a file copied in there by hand would otherwise be
cached past the deployment that replaced it.

**Exactly** eight, not "at least": the digest's own alphabet includes the
hyphen, so a length that is merely a lower bound would read the hyphens of an
ordinary name (``put-here-by-hand.js``) as part of one. A build configured for
a longer digest falls out of this and is revalidated instead -- slower by one
request and never wrong, which is the right way round for a rule about caching.
"""

FOUND = 302
"""Both redirects: the interface may move, and neither is worth caching."""

NOT_FOUND = 404
"""A path under ``/ui/`` that is not a file of the interface."""

METHOD_NOT_ALLOWED = 405
"""Anything but ``GET`` or ``HEAD``: these paths are files, and are read."""

READ_METHODS = frozenset({"GET", "HEAD"})
"""What the interface's paths serve. Everything else is ``METHOD_NOT_ALLOWED``."""

UI_METHODS = ", ".join(sorted(READ_METHODS))
"""The ``Allow`` of that refusal. Static files are read and nothing else.

``StaticFiles`` raises a bare 405, and a 405 without ``Allow`` is a refusal
that does not answer the one question it raises. It is written here rather
than asked of the mount, because the mount is a directory of files and has no
route to ask.
"""

DOT = "."
"""No path under ``/ui/`` may have a segment beginning with this. See ``hidden``."""

CONTENT_SECURITY_POLICY = (
    "default-src 'none';"
    " script-src 'self';"
    " style-src 'self' 'unsafe-inline';"
    " img-src 'self' data:;"
    " font-src 'self';"
    " connect-src 'self';"
    " base-uri 'none';"
    " form-action 'none';"
    " frame-ancestors 'none'"
)
"""The policy of ``docs/specs/frontend.md``, spelt out directive by directive.

``default-src 'none'`` is the fallback for what a page **fetches**, and the
directives after it are the ones this interface needs: scripts, styles, images,
fonts and the API it calls, all of this origin. ``'unsafe-inline'`` appears for
styles alone, because the chat library and Tailwind set element styles as they
animate; it appears for **no** script directive, and ``index.html`` carries no
inline script so that it never has to.

The last three are the ones ``default-src`` does **not** cover, which is why
they are written out. ``frame-ancestors`` is who may frame this page (nobody).
``base-uri`` is what a ``<base>`` element may set: without it, one injected tag
re-points every relative url on the page -- and this page's script and
stylesheet are relative (``base: "./"`` in ``frontend/vite.config.ts``), so
there is something real to re-point. ``form-action`` is where a form may be
submitted: this interface has no form that posts anywhere, so the honest answer
is nowhere.
"""

FRAME_OPTIONS = "DENY"
"""``frame-ancestors 'none'`` for a browser too old to read the policy above.

One header, one value, no upkeep: the two say the same thing and the older one
is what an Internet Explorer or an old Safari understands.
"""

ASSET_CACHE_CONTROL = "public, max-age=31536000, immutable"
"""A year, and never revalidated: an asset's name holds a hash of its contents.

A build that changes a file changes its name, so a cached answer can never be
the wrong one, and an upgraded deployment costs its visitors one ``index.html``.
"""

PAGE_CACHE_CONTROL = "no-store"
"""``index.html``, and every answer that stands in for it.

It is the file that names this build's assets, so a cached copy of an older one
would ask for files the new deployment no longer has.
"""

UNHASHED_CACHE_CONTROL = "no-cache"
"""A file under ``assets/`` whose name carries no hash of its contents.

Not ``no-store``: it may be kept, and it is asked about before it is used. This
build emits no such file -- Vite hashes everything it puts there -- so this is
the answer to one appearing, which is a file that can change under its own name
and must therefore be revalidated.
"""

UI_HEADERS: tuple[tuple[str, str], ...] = (
    ("content-security-policy", CONTENT_SECURITY_POLICY),
    ("x-frame-options", FRAME_OPTIONS),
)
"""Carried by every answer the interface's own paths give, refusals included."""

NOT_BUILT_STATUS = 503
"""The interface is missing, not the path: the server is answering, and says so.

A 404 would send somebody looking for a wrong URL. This is a deployment that
was installed without its interface -- or, far more often, a development
checkout where ``npm run build`` has not been run -- and it is the installation
that has to change.
"""

NOT_BUILT = (
    "The Robinauts interface is not in this installation. The API is running:"
    " /health answers, and so does /openapi.json. A release wheel always carries"
    " the built interface; a development checkout does not, and serves it with"
    " `npm run dev` from frontend/ instead."
)
"""Said on the page and in the start-up log, so one sentence explains both."""

NOT_BUILT_PAGE = f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Robinauts</title>
  </head>
  <body>
    <h1>The interface is not built</h1>
    <p>{NOT_BUILT}</p>
  </body>
</html>
"""
"""A whole page, because a browser is what asks for this path.

No script, no stylesheet and no image: it is served under the same policy as
the interface, and a page that needed an exception would be a hole kept open
for the case where something is already wrong.
"""

HTML = "text/html; charset=utf-8"


def ui_inside(package_root: Path, /) -> Path | None:
    """``<package_root>/ui`` if a build was installed into it, else ``None``.

    Judged by ``index.html`` rather than by the directory: an empty ``ui/``
    left behind by a half-finished copy is not an interface, and the answer
    this gives decides which of the two applications below is mounted.
    """
    directory = package_root / UI_DIRECTORY
    return directory if (directory / INDEX).is_file() else None


def add_ui(app: FastAPI, ui_dir: Path | None) -> None:
    """Serve the interface under ``/ui/``, and point ``/`` and ``/ui`` at it.

    ``ui_dir`` is the directory of built files, or ``None`` where there are
    none: then the same paths answer ``NOT_BUILT_PAGE``, so that the reason is
    on the screen of whoever went looking for the interface.

    The mount is not an ``APIRoute`` and cannot declare a permission, so it is
    named in ``robinauts.legacy.api.access.FRAMEWORK_PATHS`` -- which is the one list a
    reviewer reads to see everything served without a declaration.

    Both redirects answer ``HEAD`` as well as ``GET``: a browser, a health
    check and a link checker all send one, and a 405 from the front door of a
    deployment is a confusing answer to a question with an obvious one.

    Both send the visitor to ``root_path`` + ``/ui/`` rather than to ``/ui/``:
    a deployment served under a prefix (``uvicorn --root-path``) is one where
    ``/ui/`` is not a path this application is reachable at, and an absolute
    ``Location`` of it would leave the prefix behind.
    """

    @app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False, dependencies=[public()])
    async def root(request: Request) -> Response:
        """The origin's root is the interface (``docs/specs/operations.md``)."""
        return to_the_interface(request)

    @app.api_route(
        UI_MOUNT, methods=["GET", "HEAD"], include_in_schema=False, dependencies=[public()]
    )
    async def ui(request: Request) -> Response:
        """``/ui`` is the page's directory, and the page is the file inside it."""
        return to_the_interface(request)

    app.mount(UI_MOUNT, ui_app(ui_dir))


def to_the_interface(request: Request) -> Response:
    """A redirect to ``/ui/`` as this deployment is reachable at it."""
    prefix = request.scope.get("root_path", "")
    return RedirectResponse(f"{prefix}{UI_PATH}", status_code=FOUND)


def ui_app(ui_dir: Path | None) -> ASGIApp:
    """What is mounted at ``/ui``: the built files, or the page that explains."""
    inside: ASGIApp = NotBuilt() if ui_dir is None else StaticFiles(directory=ui_dir, html=True)
    return UiHeaders(NothingHidden(inside))


def hidden(inside: str) -> bool:
    """Whether any segment of a path below ``/ui/`` begins with a dot.

    ``StaticFiles`` serves what is in the directory, dotfiles and all, and a
    directory this project fills from a build is a directory something else may
    also have written to: an editor's ``.swp``, a ``.DS_Store``, a
    ``.env`` somebody put beside the bundle. None of them is part of the
    interface, and the difference between "not served" and "served to anybody"
    is worth one comparison per request.
    """
    return any(segment.startswith(DOT) for segment in inside.split("/"))


def cache_control(inside: str) -> str:
    """How long an answer may be kept, from the path **below** ``/ui/``.

    A file the build named after its own contents (``HASHED``) may be kept for
    ever and never asked about again. A file under ``assets/`` that is **not**
    named that way may be kept and must be revalidated, because it can change
    under its own name. Everything else -- the page above all -- may not be
    kept at all.
    """
    if not inside.startswith(f"/{ASSETS}/"):
        return PAGE_CACHE_CONTROL
    return ASSET_CACHE_CONTROL if HASHED.search(inside) else UNHASHED_CACHE_CONTROL


def serves_files_only(path: str) -> bool:
    """Whether ``path`` is one of the interface's, which are read and not written.

    The three of them: the root, ``/ui`` and everything under ``/ui/``. Each is
    a file or a redirect to one, each answers ``GET`` and ``HEAD`` and nothing
    else, and none of them reads a body.

    It is asked by the **request protection** (``robinauts.legacy.api.protection``),
    which stands in front of everything and refuses a write that is not JSON
    before a byte of it is read. That is the right answer for a route that
    would have read a body and the wrong one here: ``DELETE /ui/index.html``
    is not a request with the wrong content type, it is a method these paths do
    not have, and the answer that says so is the ``405`` with ``Allow`` that
    the router and the mount already give. Nothing is given up by saying so --
    a write here reaches no application service, changes nothing and is refused
    either way.
    """
    return path == "/" or path == UI_MOUNT or path.startswith(UI_PATH)


def below_the_mount(scope: Scope) -> str:
    """The path under ``/ui/`` this request is for.

    Taken from the scope's own ``root_path`` -- which the mount extended with
    ``/ui`` -- rather than by trimming a literal, so that a deployment served
    under a prefix is judged by the same rules.
    """
    return str(scope["path"])[len(str(scope.get("root_path", ""))) :]


class NothingHidden:
    """Refuse a path under ``/ui/`` with a dot-segment in it; see ``hidden``.

    In front of the static files rather than inside them, so that it holds
    whatever is mounted -- and so that a reviewer looking for "what is not
    served" finds one function rather than a flag on a dependency.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and hidden(below_the_mount(scope)):
            # The same answer as a file that is not there: whether one is
            # there is not something to tell anybody about.
            await http_refusal(NOT_FOUND)(scope, receive, send)
            return
        await self.app(scope, receive, send)


class UiHeaders:
    """Add the interface's headers to everything the mount answers.

    Middleware rather than a response class, because what is wrapped builds its
    own responses out of files on disk and there is nothing to subclass.

    ``setdefault``, not assignment: an answer that set one of these for itself
    knows something this does not -- which is how ``NotBuilt`` keeps a 503 out
    of a cache although it is asked for at the path of an asset.

    **Whatever the status.** A file that is not there is a ``404`` that
    ``StaticFiles`` *raises*, and an exception raised past this would be turned
    into a response by a handler above the mount, which cannot know the path was
    the interface's. So the refusal is built here instead -- in the project's
    own shape (``robinauts.legacy.api.errors.http_refusal``) -- and comes back with the
    policy like every other answer at that path. A browser that is handed a
    body at a path where a document could have been is a browser that should be
    told what that body may do, and the answer is nothing.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":  # pragma: no cover -- nothing else reaches a mount
            await self.app(scope, receive, send)
            return
        kept = cache_control(below_the_mount(scope))

        async def with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in UI_HEADERS:
                    headers.setdefault(name, value)
                headers.setdefault("cache-control", kept)
            await send(message)

        try:
            await self.app(scope, receive, with_headers)
        except StarletteHTTPException as refused:
            # Only what the static files raise to say "not this": a 404 for a
            # file that is not there, a 405 for a method they do not serve.
            # Anything else is a bug and goes to the handler that logs it.
            sent = dict(refused.headers or {})
            if refused.status_code == METHOD_NOT_ALLOWED:
                sent.setdefault("allow", UI_METHODS)
            await http_refusal(refused.status_code, headers=sent)(scope, receive, with_headers)


class NotBuilt:
    """The whole interface, in an installation that has none: one page, 503.

    Every path under ``/ui/`` answers it, because there is no way to tell which
    of them somebody meant: the interface routes on the fragment, which no
    server sees (``docs/specs/frontend.md``), so ``/ui/`` is the only path it
    ever has.

    It sets its own ``Cache-Control``, which is the one thing it cannot leave
    to the wrapper: the path it was asked for may be an asset's, and the rule
    about assets is about assets, not about the page that stands where one is
    missing. An installation that gains its interface must not go on answering
    this out of a cache for a year.

    **A write is still a method these paths have not got**, and is answered
    ``405`` with the same ``Allow`` the built files answer with. The interface
    being absent does not make ``DELETE /ui/index.html`` a sensible request,
    and an installation that gains its interface must not answer differently
    to one that has it: a page saying "this is not built" is an odd reply to a
    method nothing here has ever served.
    """

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["method"] not in READ_METHODS:
            await http_refusal(METHOD_NOT_ALLOWED, headers={"allow": UI_METHODS})(
                scope, receive, send
            )
            return
        response = Response(
            NOT_BUILT_PAGE,
            status_code=NOT_BUILT_STATUS,
            media_type=HTML,
            headers={"cache-control": PAGE_CACHE_CONTROL},
        )
        await response(scope, receive, send)
