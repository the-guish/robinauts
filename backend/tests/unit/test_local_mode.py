# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The local development mode: no sign-in, one real user, this machine only.

The mode exists so that a person can work on the platform without an identity
provider, and the promise is that **nothing else about the platform changes**
(``docs/specs/sign-in.md``, "Local development mode"). So these tests are
mostly about the ordinary things still happening: a ``signed_in()`` route
resolves to somebody, that somebody is a row in the store with an id that
outlives a restart, and every check on a write still bites.

The other half is what pays for it. A server that asks nobody who they are is
safe exactly as long as nobody else can reach it, so:

- it refuses to be told to serve anything but loopback, at construction, where
  no socket has been bound yet and nothing can be bound later without it;
- and it refuses, per request, anything not addressed to this machine. That is
  the one that matters: a page on the internet can point a name of its own at
  ``127.0.0.1``, and the browser will connect here and send that name in
  ``Host``. Every same-origin rule holds for it -- it really is that page's
  own origin -- so the ``Host`` header is the only thing that says otherwise.

And the mode must not be reachable by accident: it is never the default, no
environment variable turns it on, it cannot be combined with a sign-in
configuration, and its user cannot be reached by signing in.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any

import httpx
import pytest
from fastapi import FastAPI

from aio import asyncio_test
from fakes import FakeClock, MemoryConversationStore, MemoryCredentialStore
from robinauts.legacy.api import (
    BOTH_WAYS,
    CROSS_SITE_DETAIL,
    LOOPBACK_DETAIL,
    MEDIA_TYPE_DETAIL,
    create_api,
    public,
    signed_in,
)
from robinauts.legacy.app import (
    AUTH_CONFIG_VARIABLE,
    CONFIG_VARIABLE,
    DATABASE_URL_VARIABLE,
    Deployment,
    create_app,
)
from robinauts.legacy.application import SignIn
from robinauts.legacy.core import parse_sign_in_config, secret_hash
from robinauts.legacy.domain import (
    LOCAL_PROVIDER,
    LOCAL_SUBJECT,
    LOCAL_USER_NAME,
    ConfigError,
    InvalidValueError,
    LocalMode,
    ProviderConfig,
    User,
    is_loopback_bind_host,
)
from webapp import JSON, LOCAL_URL, SESSION_SECRET, WiredLocally, running, serving, wired

DATABASE_URL = "postgresql://nobody@127.0.0.1:1/never-opened"

LOCAL_HOST = "127.0.0.1:8000"
"""The authority a client on ``LOCAL_URL`` sends, and the origin it writes from."""

CONFIGURATION = """
public_url = "https://robinauts.example.com"

[providers.google]
title = "Google"
issuer = "https://accounts.google.com"
client_id = "a-client"
client_secret_env = "ROBINAUTS_GOOGLE_SECRET"

[[allow]]
provider = "google"
hosted_domain = "example.com"
"""


def reading(variables: dict[str, str]) -> Callable[[str], str | None]:
    """The environment, without one: no test here sets a real variable."""
    return variables.get


def written(tmp_path: Path) -> Path:
    path = tmp_path / "sign-in.toml"
    path.write_text(CONFIGURATION, encoding="utf-8")
    return path


def guarded(deployment: WiredLocally) -> FastAPI:
    """The real application, plus the two kinds of route this build will have.

    ``signed_in()`` has no route of its own until the conversation routes
    arrive, and the whole point of the mode is what it answers.
    """
    app = create_api(None, local=deployment.local)

    @app.get("/only-mine")
    async def only_mine(user: Annotated[User, signed_in()]) -> dict[str, str]:
        return {"user": str(user.id)}

    @app.get("/anybody", dependencies=[public()])
    async def anybody() -> dict[str, str]:
        return {"seen": "anybody"}

    return app


def browser(app: FastAPI, *, base_url: str = LOCAL_URL) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=base_url,
        follow_redirects=False,
        trust_env=False,
    )


# The mode itself: where it may be served.


@pytest.mark.parametrize(
    "host", ["127.0.0.1", "localhost", "::1", "127.9.9.9", "LOCALHOST", " 127.0.0.1 "]
)
def test_any_address_of_this_machine_may_be_served(host: str) -> None:
    assert LocalMode(host=host).serves(LocalMode(host=host).host)


def test_the_default_is_loopback() -> None:
    assert LocalMode().host == "127.0.0.1"


@pytest.mark.parametrize(
    ("given", "kept"),
    [("[::1]", "::1"), ("::0001", "::1"), ("127.0.0.1.", "127.0.0.1"), ("LocalHost", "localhost")],
)
def test_the_bind_host_is_kept_as_a_server_wants_it(given: str, kept: str) -> None:
    """No brackets, no root dot, one spelling per address: it is handed to a server."""
    assert LocalMode(host=given).host == kept


@pytest.mark.parametrize("host", ["dev.localhost", "x.y.localhost"])
def test_a_name_that_is_only_conventionally_loopback_may_not_be_bound(host: str) -> None:
    """A bind address is not left to a resolver, and ``*.localhost`` is a resolver's word.

    The request side is deliberately looser, and stays so: a person may reach
    this server by any name of theirs that arrives here, and a name that
    arrived here arrived over loopback. What may be **bound** is the stricter
    question, because that address is handed to the operating system.
    """
    with pytest.raises(InvalidValueError):
        LocalMode(host=host)

    assert LocalMode().serves(f"{host}:8000")


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "127.9.9.9", "localhost", "[::1]"])
def test_what_may_be_bound(host: str) -> None:
    assert is_loopback_bind_host(host)


@pytest.mark.parametrize(
    "host", ["dev.localhost", "0.0.0.0", "192.168.1.5", "example.com", "", "::", None, 42]
)
def test_what_may_not(host: Any) -> None:
    assert not is_loopback_bind_host(host)


@pytest.mark.parametrize(
    "host",
    ["0.0.0.0", "192.168.1.5", "example.com", "", "::", "127.0.0.1 8000", None, 42],
)
def test_a_bind_host_that_is_not_this_machine_is_refused(host: Any) -> None:
    """At construction, which is before anything could have been bound to it."""
    with pytest.raises(InvalidValueError) as raised:
        LocalMode(host=host)

    assert "loopback" in str(raised.value) or "is served on a host" in str(raised.value)


@pytest.mark.parametrize(
    "authority",
    ["localhost", "localhost:8000", "127.0.0.1:8000", "[::1]:8000", "[::1]", "dev.localhost:3000"],
)
def test_a_request_addressed_to_this_machine_is_served(authority: str) -> None:
    assert LocalMode().serves(authority)


@pytest.mark.parametrize(
    "authority",
    ["evil.example", "evil.example:8000", "192.168.1.5:8000", "", "::1:8000", "localhost.evil.com"],
)
def test_a_request_addressed_anywhere_else_is_not(authority: str) -> None:
    assert not LocalMode().serves(authority)


# The guard: who everybody is.


@asyncio_test
async def test_a_signed_in_route_resolves_to_the_local_user_with_no_cookie() -> None:
    deployment = WiredLocally()

    async with browser(guarded(deployment)) as client:
        answered = await client.get("/only-mine")

    assert answered.status_code == 200
    assert answered.json() == {"user": str(deployment.store.users[0].id)}


@asyncio_test
async def test_the_local_user_is_one_real_row_under_the_reserved_key() -> None:
    """A row, with an id, that owns what the mode creates -- not a stand-in."""
    deployment = WiredLocally()

    async with browser(guarded(deployment)) as client:
        first = await client.get("/only-mine")
        second = await client.get("/only-mine")

    held = deployment.store.users
    assert len(held) == 1
    assert (held[0].provider, held[0].subject) == (LOCAL_PROVIDER, LOCAL_SUBJECT)
    assert held[0].name == LOCAL_USER_NAME
    assert held[0].email is None
    # Created once: the second request found the row the first one made.
    assert first.json() == second.json() == {"user": str(held[0].id)}


class CountingStore(MemoryCredentialStore):
    """A store that says how often it was asked to write a user."""

    def __init__(self) -> None:
        super().__init__()
        self.upserts = 0

    async def user_at_sign_in(self, *args: Any, **changes: Any) -> User:
        self.upserts += 1
        return await super().user_at_sign_in(*args, **changes)


@asyncio_test
async def test_the_user_is_read_on_every_request_and_written_once() -> None:
    """Reading who somebody is must not be a write (``ports.user_by_key``).

    ``user_at_sign_in`` is an upsert: in PostgreSQL it rewrites the row it
    finds, so asking it per request would leave a row version and a dead tuple
    behind every time anybody read anything.
    """
    store = CountingStore()
    deployment = WiredLocally(store=store)

    async with browser(guarded(deployment)) as client:
        for _ in range(5):
            answered = await client.get("/only-mine")

    assert answered.status_code == 200
    assert store.upserts == 1


@asyncio_test
async def test_first_requests_arriving_at_once_leave_one_user() -> None:
    """All of them find nobody and all of them create; the port leaves one row."""
    deployment = WiredLocally()

    found = await asyncio.gather(*(deployment.local.user() for _ in range(5)))

    assert len({user.id for user in found}) == 1
    assert len(deployment.store.users) == 1


@asyncio_test
async def test_a_restart_finds_the_same_person() -> None:
    """The store outlives the process, so the id does; what they own is theirs."""
    store = MemoryCredentialStore()
    before = WiredLocally(store=store)
    after = WiredLocally(store=store, clock=FakeClock())

    async with browser(guarded(before)) as client:
        first = await client.get("/only-mine")
    async with browser(guarded(after)) as client:
        second = await client.get("/only-mine")

    assert first.json() == second.json()
    assert len(store.users) == 1


@asyncio_test
async def test_a_session_cookie_is_neither_needed_nor_read() -> None:
    """There is no sign-in, so a cookie from anywhere stands for nobody."""
    deployment = WiredLocally()

    async with browser(guarded(deployment)) as client:
        answered = await client.get(
            "/only-mine", headers={"cookie": f"__Host-robinauts_session={SESSION_SECRET}"}
        )

    assert answered.json() == {"user": str(deployment.store.users[0].id)}


# What the interface is told.


@asyncio_test
async def test_the_session_route_says_sign_in_is_off_and_names_the_local_user() -> None:
    deployment = WiredLocally()

    async with serving(None, local=deployment.local, base_url=LOCAL_URL) as client:
        answered = await client.get("/auth/session")

    body = answered.json()
    assert answered.status_code == 200
    assert (body["sign_in"], body["local_development"]) == (False, True)
    assert body["providers"] == []
    assert body["user"] == {
        "id": str(deployment.store.users[0].id),
        "name": LOCAL_USER_NAME,
        "email": None,
        "provider": LOCAL_PROVIDER,
    }
    assert answered.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("route", ["/auth/login/google", "/auth/callback/google"])
@asyncio_test
async def test_the_navigation_routes_land_in_the_interface(route: str) -> None:
    """Nothing to begin and nothing to finish, and no crash either."""
    async with serving(None, local=WiredLocally().local, base_url=LOCAL_URL) as client:
        answered = await client.get(route)

    assert answered.status_code == 303
    assert answered.headers["location"] == "/ui/"


@asyncio_test
async def test_signing_out_is_a_harmless_204() -> None:
    async with serving(None, local=WiredLocally().local, base_url=LOCAL_URL) as client:
        answered = await client.post(
            "/auth/logout", headers={**JSON, "origin": f"http://{LOCAL_HOST}"}
        )

    assert answered.status_code == 204
    assert "set-cookie" not in answered.headers


# Addressed to this machine, or not served at all.


@pytest.mark.parametrize("authority", ["localhost:8000", "127.0.0.1:8000", "[::1]:8000"])
@asyncio_test
async def test_every_loopback_spelling_reaches_the_routes(authority: str) -> None:
    deployment = WiredLocally()

    async with browser(guarded(deployment)) as client:
        answered = await client.get("/only-mine", headers={"host": authority})

    assert answered.status_code == 200


@pytest.mark.parametrize("route", ["/only-mine", "/anybody"])
@asyncio_test
async def test_a_request_addressed_elsewhere_is_refused_read_or_not(route: str) -> None:
    """DNS rebinding: the browser connected here, and named somebody else.

    It is a read, which is the case a cross-site rule would not have covered:
    the page owns that origin, so its own fetch is same-origin to the browser.
    """
    async with browser(guarded(WiredLocally())) as client:
        answered = await client.get(route, headers={"host": "evil.example"})

    assert answered.status_code == 403
    assert answered.json() == {"error": "CrossSiteRequestError", "detail": LOOPBACK_DETAIL}


NO_SERVER = object()
"""What a scope that says nothing about where it answered is written with."""


async def status_of(app: FastAPI, headers: list[tuple[bytes, bytes]], server: Any) -> int:
    """Drive the real application with a scope of our own, and answer its status.

    Two of the things this mode refuses cannot be sent by a client: a second
    ``Host`` header, and an address the server answered on. Both are in the
    scope, so the scope is what is written here.
    """
    answered: dict[str, Any] = {}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            answered.update(message)

    async def receive() -> dict[str, Any]:  # pragma: no cover -- no body is read
        return {"type": "http.request", "body": b"", "more_body": False}

    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "path": "/anybody",
        "raw_path": b"/anybody",
        "query_string": b"",
        "root_path": "",
        "scheme": "http",
        "headers": headers,
        "server": server,
        "client": ("127.0.0.1", 5000),
        "app": app,
    }
    if server is NO_SERVER:
        del scope["server"]
    await app(scope, receive, send)
    return int(answered["status"])


@asyncio_test
async def test_a_scope_that_is_addressed_to_this_machine_is_served() -> None:
    assert await status_of(guarded(WiredLocally()), [(b"host", b"[::1]")], ("::1", 8000)) == 200


@asyncio_test
async def test_two_host_headers_are_refused() -> None:
    """One is read here and the other by whatever is in front of or behind us."""
    sent = [(b"host", b"127.0.0.1:8000"), (b"host", b"evil.example")]

    assert await status_of(guarded(WiredLocally()), sent, ("127.0.0.1", 8000)) == 403


@asyncio_test
async def test_a_unix_socket_is_this_machine_by_nature() -> None:
    """There is no interface to reach it from: whoever opens the file is here."""
    sent = [(b"host", b"localhost")]

    assert await status_of(guarded(WiredLocally()), sent, ("/run/robinauts.sock", None)) == 200


@pytest.mark.parametrize("server", [NO_SERVER, None, (), ("127.0.0.1",)])
@asyncio_test
async def test_a_scope_that_does_not_say_where_it_answered_is_refused(server: Any) -> None:
    """Fail closed: the mode's safety is that it cannot be reached from elsewhere."""
    sent = [(b"host", b"127.0.0.1:8000")]

    assert await status_of(guarded(WiredLocally()), sent, server) == 403


@asyncio_test
async def test_a_server_answering_off_loopback_is_refused() -> None:
    """The other half of the rule: what the process bound, not what was named.

    The mode refuses a non-loopback bind host at construction; this is what
    catches a server started some other way, with a ``Host`` that says nothing
    wrong.
    """
    sent = [(b"host", b"localhost:8000")]

    assert await status_of(guarded(WiredLocally()), sent, ("192.168.1.5", 8000)) == 403


# The checks on writes, which stay on.


@asyncio_test
async def test_a_write_from_the_origin_it_is_served_on_is_taken() -> None:
    async with serving(None, local=WiredLocally().local, base_url=LOCAL_URL) as client:
        answered = await client.post(
            "/auth/logout",
            headers={**JSON, "origin": f"http://{LOCAL_HOST}", "sec-fetch-site": "same-origin"},
        )

    assert answered.status_code == 204


@asyncio_test
async def test_a_write_with_no_origin_needs_the_browser_to_say_same_origin() -> None:
    """Every request here is authenticated, so every write is a credentialed one."""
    async with serving(None, local=WiredLocally().local, base_url=LOCAL_URL) as client:
        without = await client.post("/auth/logout", headers=JSON)
        said = await client.post("/auth/logout", headers={**JSON, "sec-fetch-site": "same-origin"})

    assert without.status_code == 403
    assert without.json() == {"error": "CrossSiteRequestError", "detail": CROSS_SITE_DETAIL}
    assert said.status_code == 204


@pytest.mark.parametrize(
    "origin",
    [
        "http://127.0.0.1:9000",
        "http://localhost:8000",
        "https://127.0.0.1:8000",
        "http://evil.example",
        "null",
        "",
    ],
)
@asyncio_test
async def test_a_write_from_another_origin_is_refused(origin: str) -> None:
    """Another port of this machine is another origin, and so is another scheme.

    ``http://localhost:8000`` is among them because this client is served on
    ``127.0.0.1:8000`` and names it: a page is compared with the origin the
    request itself was addressed to, not with every name this machine has.
    """
    async with serving(None, local=WiredLocally().local, base_url=LOCAL_URL) as client:
        answered = await client.post("/auth/logout", headers={**JSON, "origin": origin})

    assert answered.status_code == 403


@asyncio_test
async def test_a_write_is_matched_against_the_host_it_was_addressed_to() -> None:
    """Served as ``localhost`` and written from ``localhost``: the same origin."""
    served_as = "http://localhost:8000"
    async with serving(None, local=WiredLocally().local, base_url=served_as) as client:
        answered = await client.post("/auth/logout", headers={**JSON, "origin": served_as})

    assert answered.status_code == 204


@asyncio_test
async def test_a_write_must_still_be_json() -> None:
    async with serving(None, local=WiredLocally().local, base_url=LOCAL_URL) as client:
        answered = await client.post(
            "/auth/logout",
            headers={"content-type": "text/plain", "origin": f"http://{LOCAL_HOST}"},
        )

    assert answered.status_code == 415
    assert answered.json() == {"error": "UnsupportedMediaTypeError", "detail": MEDIA_TYPE_DETAIL}


@asyncio_test
async def test_a_cross_site_write_is_still_refused() -> None:
    async with serving(None, local=WiredLocally().local, base_url=LOCAL_URL) as client:
        answered = await client.post(
            "/auth/logout",
            headers={**JSON, "origin": f"http://{LOCAL_HOST}", "sec-fetch-site": "cross-site"},
        )

    assert answered.status_code == 403
    assert answered.json() == {"error": "CrossSiteRequestError", "detail": CROSS_SITE_DETAIL}


# How it is asked for, and how it is not.


@asyncio_test
async def test_the_mode_is_not_the_default(tmp_path: Path) -> None:
    app = create_app(
        config_path=written(tmp_path),
        secret_for=reading({"ROBINAUTS_GOOGLE_SECRET": "a-secret"}),
        credentials=MemoryCredentialStore(),
        conversation_store=MemoryConversationStore(),
        clock=FakeClock(),
    )

    async with running(app):
        assert app.state.local is None
        assert app.state.sign_in is not None


def test_no_environment_variable_can_switch_it_on(tmp_path: Path) -> None:
    """The mode is asked for in the command that starts the server, and nowhere else.

    A variable that turned sign-in off would be one stale export, one unit
    file or one inherited container environment away from a deployment that
    lets everybody in as one person. So the composition root is shown here to
    read **two** names and no others, whatever else is in the environment.
    """
    tempting = {
        AUTH_CONFIG_VARIABLE: str(written(tmp_path)),
        DATABASE_URL_VARIABLE: DATABASE_URL,
        "ROBINAUTS_GOOGLE_SECRET": "a-secret",
        "ROBINAUTS_LOCAL_DEVELOPMENT": "1",
        "ROBINAUTS_DEV_NO_SIGN_IN": "yes",
        "ROBINAUTS_DEV_MODE": "true",
        "ROBINAUTS_NO_SIGN_IN": "1",
    }
    asked: list[str] = []

    def secret_for(name: str) -> str | None:
        asked.append(name)
        return tempting.get(name)

    deployment = Deployment.configured(
        secret_for=secret_for,
        credentials=MemoryCredentialStore(),
        conversation_store=MemoryConversationStore(),
    )

    assert deployment.local_mode is None
    assert set(asked) == {
        CONFIG_VARIABLE,
        # Read too, and only because it is the old name of the one above.
        AUTH_CONFIG_VARIABLE,
        DATABASE_URL_VARIABLE,
        "ROBINAUTS_GOOGLE_SECRET",
    }


def test_it_cannot_be_combined_with_a_sign_in_configuration(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as raised:
        Deployment.configured(
            config_path=written(tmp_path),
            local_development_host="127.0.0.1",
            database_url=DATABASE_URL,
            secret_for=reading({}),
            credentials=MemoryCredentialStore(),
            conversation_store=MemoryConversationStore(),
        )

    assert any("cannot be combined" in problem for problem in raised.value.problems)
    assert any(AUTH_CONFIG_VARIABLE in problem for problem in raised.value.problems)


def test_a_configuration_in_the_environment_counts_as_asking_for_both(tmp_path: Path) -> None:
    """Which is what makes the refusal worth something: the file is usually named there."""
    with pytest.raises(ConfigError) as raised:
        Deployment.configured(
            local_development_host="127.0.0.1",
            database_url=DATABASE_URL,
            secret_for=reading({AUTH_CONFIG_VARIABLE: str(written(tmp_path))}),
            credentials=MemoryCredentialStore(),
            conversation_store=MemoryConversationStore(),
        )

    assert any("cannot be combined" in problem for problem in raised.value.problems)


def test_a_name_only_a_resolver_calls_loopback_is_refused_at_start_up() -> None:
    with pytest.raises(ConfigError) as raised:
        Deployment.configured(
            local_development_host="dev.localhost",
            database_url=DATABASE_URL,
            secret_for=reading({}),
            credentials=MemoryCredentialStore(),
            conversation_store=MemoryConversationStore(),
        )

    assert any("loopback interface only" in problem for problem in raised.value.problems)


def test_a_non_loopback_bind_host_is_refused_at_start_up() -> None:
    """Beside every other start-up problem, as one ``ConfigError``."""
    with pytest.raises(ConfigError) as raised:
        Deployment.configured(
            local_development_host="0.0.0.0",
            secret_for=reading({}),
        )

    problems = "\n".join(raised.value.problems)
    assert "loopback interface only" in problems
    assert "0.0.0.0" in problems
    assert DATABASE_URL_VARIABLE in problems


def test_a_deployment_is_one_mode_or_the_other() -> None:
    with pytest.raises(ConfigError):
        Deployment()

    with pytest.raises(ConfigError):
        Deployment(wired().config, local_development_host="127.0.0.1")


def test_an_application_serves_one_mode_or_the_other() -> None:
    """The door behind the composition root's refusal."""
    with pytest.raises(ConfigError) as raised:
        create_api(wired().sign_in, local=WiredLocally().local)

    assert raised.value.problems == (BOTH_WAYS,)


@asyncio_test
async def test_start_up_warns_once_that_sign_in_is_off(caplog: Any) -> None:
    app = create_app(
        local_development_host="127.0.0.1",
        secret_for=reading({}),
        credentials=MemoryCredentialStore(),
        conversation_store=MemoryConversationStore(),
        clock=FakeClock(),
    )

    with caplog.at_level(logging.WARNING, logger="robinauts.legacy.app"):
        async with running(app):
            async with browser(app) as client:
                await client.get("/auth/session")
                await client.get("/auth/session")

    said = [record for record in caplog.records if "SIGN-IN IS OFF" in record.getMessage()]
    assert len(said) == 1
    assert said[0].levelno == logging.WARNING
    assert "127.0.0.1" in said[0].getMessage()
    assert LOCAL_PROVIDER in said[0].getMessage()


@asyncio_test
async def test_the_mode_builds_no_identity_provider(tmp_path: Path) -> None:
    """There is nobody to talk to, and a client nothing uses is one to close."""
    deployment = Deployment.configured(
        local_development_host="127.0.0.1",
        secret_for=reading({}),
        credentials=MemoryCredentialStore(),
        conversation_store=MemoryConversationStore(),
        clock=FakeClock(),
    )

    assert await deployment.open() is None
    assert deployment.sign_in is None
    assert deployment.local_access is not None
    await deployment.aclose()
    assert deployment.local_access is None


# The reserved identity: nobody else may be this person.


def test_no_provider_may_be_configured_with_the_reserved_id() -> None:
    """The id is not spelt like a provider id, and both layers hold to the shape."""
    with pytest.raises(ConfigError) as refused_file:
        parse_sign_in_config(
            {
                "public_url": "https://robinauts.example.com",
                "providers": {
                    LOCAL_PROVIDER: {
                        "title": "Not Google",
                        "issuer": "https://accounts.google.com",
                        "client_id": "a-client",
                        "client_secret_env": "ROBINAUTS_SECRET",
                    }
                },
                "allow": [{"provider": LOCAL_PROVIDER, "hosted_domain": "example.com"}],
            }
        )

    with pytest.raises(InvalidValueError):
        ProviderConfig(
            id=LOCAL_PROVIDER,
            title="Not Google",
            issuer="https://accounts.google.com",
            client_id="a-client",
            client_secret_env="ROBINAUTS_SECRET",
        )

    # The reserved id is the file's one problem: the entry is one Google takes.
    (problem,) = refused_file.value.problems
    assert LOCAL_PROVIDER in problem


@asyncio_test
async def test_a_session_that_names_the_local_user_signs_nobody_in() -> None:
    """A database kept from a run of the mode does not hand anybody that account."""
    deployment = wired()
    now = deployment.clock.now()
    local = await deployment.store.user_at_sign_in(
        LOCAL_PROVIDER, LOCAL_SUBJECT, name=LOCAL_USER_NAME, email=None, now=now
    )
    await deployment.store.add_session(
        secret_hash(SESSION_SECRET),
        local.id,
        created_at=now,
        expires_at=now + deployment.config.session_life,
    )
    sign_in: SignIn = deployment.sign_in

    assert await sign_in.resolve_session(SESSION_SECRET) is None
