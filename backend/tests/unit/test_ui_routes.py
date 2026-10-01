# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Serving the built interface: the page, the assets, and what carries them.

Two applications are exercised here and they are the two a deployment can be:
one with a directory of built files -- written into a temporary directory, so
that what is asserted is the serving and not this checkout's own ``npm run
build`` -- and one with none, which is a development checkout and answers the
page that says so.

The policy of ``docs/specs/frontend.md`` is compared **exactly**, directive by
directive: it is the one header a reviewer of this project is asked to read,
and a test that only looked for ``default-src`` would pass a policy that had
quietly grown ``script-src 'unsafe-eval'``.

Traversal is asked as a **raw scope**. ``httpx`` resolves ``..`` out of a URL
before it sends one, so a client cannot ask the question at all: what is driven
here is the ASGI application with the path a server would really hand it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest

from aio import asyncio_test
from robinauts.legacy.api import (
    ASSET_CACHE_CONTROL,
    ASSETS,
    CONTENT_SECURITY_POLICY,
    FOUND,
    FRAME_OPTIONS,
    INDEX,
    NOT_BUILT,
    NOT_BUILT_STATUS,
    PAGE_CACHE_CONTROL,
    SECURITY_HEADERS,
    UI_DIRECTORY,
    UI_MOUNT,
    UI_PATH,
    UNHASHED_CACHE_CONTROL,
    cache_control,
    create_api,
    hidden,
    openapi_document,
    ui_inside,
)
from webapp import PUBLIC_URL, client_on

PAGE = "<!doctype html><title>Robinauts</title><script src='./assets/index-D93cLA-w.js'></script>"
"""What the build writes, in miniature: a page that names a hashed asset."""

ASSET = "index-D93cLA-w.js"
"""A file name shaped as Vite writes one: the name, the digest, the extension.

Taken from a real build, so that what the caching rule is tested against is
what the build really emits rather than what this test imagines it does.
"""

BY_HAND = "put-here-by-hand.js"
"""A file under ``assets/`` whose name carries no digest. Nothing builds one.

It is the case the rule exists for: a name that can mean two different files
on two different days must not be kept for a year.
"""

A_DOTFILE = ".env"
"""Something else's file, in the directory the build writes into."""

SECRET_IN_A_DOTFILE = "ROBINAUTS_DATABASE_URL=postgresql://someone:hunter2@db/robinauts"

SCRIPT = "console.log('robinauts')"

POLICY = (
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
"""``docs/specs/frontend.md``, written out again rather than imported.

A test that imported the constant it is checking would pass whatever the
constant said. This is the specification, copied by hand, and a change to the
policy is a change to this line and something a reviewer reads.
"""


@pytest.fixture
def built(tmp_path: Path) -> Path:
    """A directory shaped like ``frontend/dist``, and what else may land in one.

    The page, a hashed asset, an asset that is not hashed, and a dotfile: the
    last two are not what a build writes, and are exactly what a directory on a
    real machine grows.
    """
    (tmp_path / INDEX).write_text(PAGE, encoding="utf-8")
    (tmp_path / A_DOTFILE).write_text(SECRET_IN_A_DOTFILE, encoding="utf-8")
    assets = tmp_path / ASSETS
    assets.mkdir()
    (assets / ASSET).write_text(SCRIPT, encoding="utf-8")
    (assets / BY_HAND).write_text(SCRIPT, encoding="utf-8")
    (assets / A_DOTFILE).write_text(SECRET_IN_A_DOTFILE, encoding="utf-8")
    return tmp_path


@asynccontextmanager
async def showing(ui_dir: Path | None) -> AsyncIterator[httpx.AsyncClient]:
    """A client on the real application, serving ``ui_dir`` or nothing."""
    async with client_on(create_api(ui_dir=ui_dir), base_url=PUBLIC_URL) as client:
        yield client


@asyncio_test
async def test_the_page_is_served_at_the_path_with_the_slash(built: Path) -> None:
    async with showing(built) as client:
        answer = await client.get(UI_PATH)

    assert answer.status_code == 200
    assert answer.text == PAGE
    assert answer.headers["content-type"].startswith("text/html")


@asyncio_test
async def test_the_path_without_the_slash_leads_to_the_one_with_it(built: Path) -> None:
    # The built page names its assets relatively, so it has to be served from
    # the directory they are in: a copy of it at /ui would ask for /assets/.
    async with showing(built) as client:
        answer = await client.get(UI_MOUNT)

    assert answer.status_code == FOUND
    assert answer.headers["location"] == UI_PATH


@asyncio_test
async def test_the_root_of_the_origin_leads_to_the_interface(built: Path) -> None:
    # A deployment is served at the root of an origin (docs/specs/operations.md),
    # so somebody who types the host name is asking for the interface.
    async with showing(built) as client:
        answer = await client.get("/")

    assert answer.status_code == FOUND
    assert answer.headers["location"] == UI_PATH


@pytest.mark.parametrize("path", ["/", UI_MOUNT])
@asyncio_test
async def test_a_head_of_either_door_is_answered_like_a_get(built: Path, path: str) -> None:
    # A browser, a health check and a link checker all send one, and 405 from
    # the front door of a deployment is a confusing answer to an easy question.
    async with showing(built) as client:
        answer = await client.head(path)

    assert answer.status_code == FOUND
    assert answer.headers["location"] == UI_PATH
    assert answer.content == b""


@pytest.mark.parametrize("path", ["/", UI_MOUNT])
@asyncio_test
async def test_a_redirect_keeps_the_prefix_the_deployment_is_served_under(
    built: Path, path: str
) -> None:
    """``uvicorn --root-path /robinauts``: ``/ui/`` is not a path we answer at.

    An absolute ``Location`` of ``/ui/`` would send the browser out of the
    prefix, to a path of whatever else is served at the root of that origin.
    """
    prefix = "/robinauts"
    app = create_api(ui_dir=built)

    status, _, headers = await asked(app, f"{prefix}{path}", root_path=prefix)

    assert status == FOUND
    assert headers[b"location"] == f"{prefix}{UI_PATH}".encode()


@asyncio_test
async def test_an_asset_the_build_hashed_may_be_kept_for_a_year(built: Path) -> None:
    async with showing(built) as client:
        answer = await client.get(f"{UI_PATH}{ASSETS}/{ASSET}")

    assert answer.status_code == 200
    assert answer.text == SCRIPT
    assert answer.headers["cache-control"] == ASSET_CACHE_CONTROL
    assert "immutable" in ASSET_CACHE_CONTROL


@asyncio_test
async def test_an_asset_that_carries_no_hash_is_revalidated(built: Path) -> None:
    # It can be a different file tomorrow under the same name, so a year of it
    # would be a year of the wrong one.
    async with showing(built) as client:
        answer = await client.get(f"{UI_PATH}{ASSETS}/{BY_HAND}")

    assert answer.status_code == 200
    assert answer.headers["cache-control"] == UNHASHED_CACHE_CONTROL
    assert "immutable" not in UNHASHED_CACHE_CONTROL


@pytest.mark.parametrize(
    ("path", "kept"),
    [
        (f"/{ASSETS}/index-D93cLA-w.js", ASSET_CACHE_CONTROL),
        (f"/{ASSETS}/index-BlZp0KKD.css", ASSET_CACHE_CONTROL),
        (f"/{ASSETS}/nested/logo-A1b2C3d4.svg", ASSET_CACHE_CONTROL),
        (f"/{ASSETS}/{BY_HAND}", UNHASHED_CACHE_CONTROL),
        (f"/{ASSETS}/index.js", UNHASHED_CACHE_CONTROL),
        # Seven characters is short of the digest Vite writes, and nine is past
        # it: the hyphen is in the digest's own alphabet, so a bound rather
        # than a length would read an ordinary hyphenated name as a digest.
        (f"/{ASSETS}/index-D93cLA.js", UNHASHED_CACHE_CONTROL),
        (f"/{ASSETS}/index-0123456789abcdef.js", UNHASHED_CACHE_CONTROL),
        # A hyphen and eight characters, but not at the end: the digest Vite
        # appends is the last thing before the extension.
        (f"/{ASSETS}/index-D93cLA-w.js.map.txt", UNHASHED_CACHE_CONTROL),
        ("/", PAGE_CACHE_CONTROL),
        (f"/{INDEX}", PAGE_CACHE_CONTROL),
        # Not under assets/, whatever it is called.
        ("/index-D93cLA-w.js", PAGE_CACHE_CONTROL),
        (f"/{ASSETS}", PAGE_CACHE_CONTROL),
    ],
)
def test_how_long_each_kind_of_file_may_be_kept(path: str, kept: str) -> None:
    assert cache_control(path) == kept


@asyncio_test
async def test_the_page_itself_is_never_kept(built: Path) -> None:
    # It is the file that names this build's hashed assets, so a cached copy of
    # an older one would ask an upgraded deployment for files it no longer has.
    async with showing(built) as client:
        answer = await client.get(UI_PATH)

    assert answer.headers["cache-control"] == PAGE_CACHE_CONTROL


@pytest.mark.parametrize(
    "path",
    [
        UI_PATH,
        f"{UI_PATH}{ASSETS}/{ASSET}",
        # A file that is not there, and one that is refused: both are answers
        # at a path where a document could have been.
        f"{UI_PATH}never-built.js",
        f"{UI_PATH}{A_DOTFILE}",
    ],
)
@asyncio_test
async def test_every_answer_of_the_interface_carries_the_policy(built: Path, path: str) -> None:
    async with showing(built) as client:
        answer = await client.get(path)

    assert answer.headers["content-security-policy"] == POLICY
    assert CONTENT_SECURITY_POLICY == POLICY
    assert answer.headers["x-frame-options"] == FRAME_OPTIONS
    # And what every answer of this application carries anyway.
    for name, value in SECURITY_HEADERS:
        assert answer.headers[name] == value


@asyncio_test
async def test_the_api_does_not_carry_the_interfaces_policy(built: Path) -> None:
    # The policy governs a document with scripts in it. A JSON body has none,
    # and a header on it would be one more thing to keep in step for nothing.
    async with showing(built) as client:
        answer = await client.get("/health")

    assert answer.status_code == 200
    assert "content-security-policy" not in answer.headers
    assert answer.headers["x-content-type-options"] == "nosniff"


@asyncio_test
async def test_a_head_of_the_page_answers_the_headers_and_no_body(built: Path) -> None:
    async with showing(built) as client:
        answer = await client.head(UI_PATH)

    assert answer.status_code == 200
    assert answer.content == b""
    assert answer.headers["content-length"] == str(len(PAGE))
    assert answer.headers["content-security-policy"] == POLICY


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
@asyncio_test
async def test_a_write_to_a_file_is_the_method_it_has_not_got(built: Path, method: str) -> None:
    """405 with an ``Allow``, and not 415 about a body nobody would have read.

    The request protection stands in front of everything and refuses a write
    that is not JSON before a byte of it is read, which is right for a route
    that reads bodies and wrong here: these paths are files. It lets the
    interface's own paths past the write checks
    (``robinauts.legacy.api.ui.serves_files_only``) so that the mount answers what is
    really the matter.
    """
    async with showing(built) as client:
        answer = await client.request(method, f"{UI_PATH}{INDEX}")

    assert answer.status_code == 405
    assert answer.headers["allow"] == "GET, HEAD"
    # And the interface's headers, like every other answer at that path.
    assert answer.headers["content-security-policy"] == POLICY


@asyncio_test
async def test_a_write_to_the_page_itself_is_answered_the_same_way(built: Path) -> None:
    # `/ui` and `/` are routes rather than files, so FastAPI answers them --
    # but only because the protection let the request through to it.
    async with showing(built) as client:
        answer = await client.post(UI_MOUNT)

    assert answer.status_code == 405
    assert set(answer.headers["allow"].replace(" ", "").split(",")) == {"GET", "HEAD"}


@asyncio_test
async def test_a_write_to_the_api_is_still_refused_for_what_it_carries(built: Path) -> None:
    # The rule is about the interface's paths and nothing else: a write to the
    # API with no content type is refused before a byte of it is read.
    async with showing(built) as client:
        answer = await client.post("/api/turns", content=b"{}")

    assert answer.status_code == 415


@asyncio_test
async def test_a_file_that_is_not_there_is_not_found(built: Path) -> None:
    async with showing(built) as client:
        answer = await client.get(f"{UI_PATH}{ASSETS}/never-built.js")

    assert answer.status_code == 404
    # The project's shape, not Starlette's, exactly as every other refusal of
    # this application: one answer, wherever it was built.
    assert set(answer.json()) == {"error", "detail"}


@pytest.mark.parametrize(
    "path",
    [
        f"{UI_PATH}{A_DOTFILE}",
        f"{UI_PATH}{ASSETS}/{A_DOTFILE}",
        f"{UI_PATH}.well-known/anything",
        f"{UI_PATH}{ASSETS}/.hidden/thing.js",
    ],
)
@asyncio_test
async def test_nothing_whose_name_begins_with_a_dot_is_served(built: Path, path: str) -> None:
    """A directory a build writes into is one other things write into too.

    ``StaticFiles`` would hand out a ``.env`` left beside the bundle to
    anybody who asked for it by name, and a deployment's ``.env`` is where its
    database url is. The answer is the one a file that is not there gets, so
    that asking does not say whether there is one.
    """
    async with showing(built) as client:
        answer = await client.get(path)

    assert answer.status_code == 404
    assert SECRET_IN_A_DOTFILE not in answer.text
    assert "hunter2" not in answer.text


@pytest.mark.parametrize(
    ("path", "refused"),
    [
        ("/index.html", False),
        (f"/{ASSETS}/index-D93cLA-w.js", False),
        ("/.env", True),
        (f"/{ASSETS}/.env", True),
        ("/.well-known/security.txt", True),
        ("/a/.git/config", True),
        # A name with a dot in it, rather than at the start of a segment.
        ("/robots.txt", False),
        ("/a.b/c", False),
    ],
)
def test_which_paths_count_as_hidden(path: str, refused: bool) -> None:
    assert hidden(path) is refused


@pytest.mark.parametrize(
    "path",
    [
        f"{UI_PATH}../pyproject.toml",
        f"{UI_PATH}{ASSETS}/../../pyproject.toml",
        "/ui/....//pyproject.toml",
        f"{UI_PATH}/etc/passwd",
    ],
)
@asyncio_test
async def test_nothing_outside_the_built_directory_is_reachable(built: Path, path: str) -> None:
    """Starlette refuses these; asked anyway, because the answer is what matters.

    A directory of files served to anybody is the one place in this application
    where a path from a request reaches the file system, so the rule is tested
    here rather than believed of a dependency.
    """
    outside = built.parent / "pyproject.toml"
    outside.write_text("[project]\nname = 'not yours'\n", encoding="utf-8")
    app = create_api(ui_dir=built)

    status, body, _ = await asked(app, path)

    assert status == 404, body
    assert b"not yours" not in body


@asyncio_test
async def test_an_installation_with_no_interface_says_so_on_the_page() -> None:
    async with showing(None) as client:
        answer = await client.get(UI_PATH)

    assert answer.status_code == NOT_BUILT_STATUS
    assert answer.headers["content-type"].startswith("text/html")
    assert NOT_BUILT in answer.text
    assert answer.headers["content-security-policy"] == POLICY


@asyncio_test
async def test_the_page_that_says_so_is_never_cached_even_at_an_assets_path() -> None:
    # A **hashed** asset's path, which is the one the caching rule would
    # otherwise answer with a year of `immutable` -- and the answer is not an
    # asset at all: it would outlive the installation that gained its
    # interface.
    path = f"{UI_PATH}{ASSETS}/{ASSET}"
    assert cache_control(f"/{ASSETS}/{ASSET}") == ASSET_CACHE_CONTROL

    async with showing(None) as client:
        answer = await client.get(path)

    assert answer.status_code == NOT_BUILT_STATUS
    assert answer.headers["cache-control"] == PAGE_CACHE_CONTROL


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
@asyncio_test
async def test_a_write_is_the_method_it_has_not_got_there_too(method: str) -> None:
    # An installation without its interface must answer a write the way one
    # with it does: the interface being absent does not make
    # `DELETE /ui/index.html` a sensible request, and a page saying "this is
    # not built" is an odd reply to a method nothing here has ever served.
    async with showing(None) as client:
        answer = await client.request(method, f"{UI_PATH}{INDEX}")

    assert answer.status_code == 405
    assert answer.headers["allow"] == "GET, HEAD"
    assert answer.headers["content-security-policy"] == POLICY


@asyncio_test
async def test_an_installation_with_no_interface_still_serves_the_api() -> None:
    # A screen nobody built is not a reason for a deployment to be down, and a
    # development checkout is exactly this application.
    async with showing(None) as client:
        answer = await client.get("/health")
        led = await client.get("/")

    assert answer.status_code == 200
    assert led.status_code == FOUND


def test_a_directory_with_no_page_in_it_is_no_interface(tmp_path: Path) -> None:
    # An empty ui/ -- a copy that stopped half way -- is not something to serve
    # a 404 out of: it is an installation without an interface.
    (tmp_path / UI_DIRECTORY).mkdir()

    assert ui_inside(tmp_path) is None

    (tmp_path / UI_DIRECTORY / INDEX).write_text(PAGE, encoding="utf-8")

    assert ui_inside(tmp_path) == tmp_path / UI_DIRECTORY


def test_nothing_the_interface_adds_is_in_the_openapi_document() -> None:
    # The document describes the API a client is generated from; the two
    # redirects and the directory of files are browser navigations and files.
    paths = openapi_document()["paths"]

    assert "/" not in paths
    assert UI_MOUNT not in paths
    assert UI_PATH not in paths


async def asked(
    app: Any, path: str, *, root_path: str = ""
) -> tuple[int, bytes, dict[bytes, bytes]]:
    """Drive ``app`` with a scope of this test's own, and read the whole answer.

    A client is not enough for two of the questions here: ``httpx`` resolves
    ``..`` out of a URL before it sends one, and ``root_path`` is something a
    **server** puts in the scope and no request can carry.
    """
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "https",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": root_path,
        "headers": [(b"host", b"robinauts.example.com")],
        "client": ("127.0.0.1", 51234),
        "server": ("robinauts.example.com", 443),
    }
    sent: list[Any] = []

    async def receive() -> Any:
        return {"type": "http.disconnect"}

    async def send(message: Any) -> None:
        sent.append(message)

    await app(scope, receive, send)
    start = next(message for message in sent if message["type"] == "http.response.start")
    body = b"".join(
        message.get("body", b"") for message in sent if message["type"] == "http.response.body"
    )
    return start["status"], body, {name: value for name, value in start["headers"]}
