# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The four ``/auth`` routes, over the real application and the fakes.

What is proved here is what the routes add to a sign-in that
``tests/unit/test_signin_flow.py`` has already proved: the cookies and their
attributes, the redirects, which query parameter goes where, and that every
way a sign-in can fail ends at the sign-in page with a code and no cookie.
The identity provider is scripted rather than reached
(``tests/integration/test_api_sign_in.py`` drives the same routes against a
provider on a socket).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

import httpx
import pytest

from aio import asyncio_test
from fakes import MemoryCredentialStore
from robinauts.legacy.api import SIGN_IN_PAGE, login_cookie, session_cookie
from robinauts.legacy.domain import ProviderUnavailableError, SignInErrorCode
from standin import unsigned_jwt
from webapp import (
    GOOGLE,
    JSON,
    LOOPBACK_URL,
    PUBLIC_URL,
    Wired,
    cookie,
    open_session,
    serving,
    sign_in_config,
    wired,
)

LOGIN_SECONDS = 600
"""Ten minutes: how long a sign-in may take (``domain.PENDING_LOGIN_MINUTES``)."""

SESSION_SECONDS = 12 * 3600
"""The default ``session_hours``."""


def set_cookies(response: httpx.Response) -> dict[str, tuple[str, str]]:
    """Every ``Set-Cookie`` of an answer: what it sets, and its attributes.

    The value as it was written, the attributes lower-cased -- a browser reads
    ``HttpOnly`` and ``httponly`` alike, and a test that insisted on one
    spelling would be testing the framework's taste.
    """
    found: dict[str, tuple[str, str]] = {}
    for header in response.headers.get_list("set-cookie"):
        pair, _, attributes = header.partition(";")
        name, _, value = pair.partition("=")
        found[name.strip()] = (value.strip(), attributes.lower())
    return found


def query_of(url: str, name: str) -> str | None:
    values = parse_qs(urlsplit(url).query).get(name)
    return values[0] if values else None


def token_for(deployment: Wired, *, nonce: str | None, **claims: Any) -> dict[str, str]:
    """A token response carrying an ID token for this sign-in."""
    now = int(deployment.clock.now().timestamp())
    payload: dict[str, Any] = {
        "iss": GOOGLE.issuer,
        "aud": GOOGLE.client_id,
        "iat": now,
        "exp": now + 300,
        "nonce": nonce,
        "sub": "248289761001",
        "name": "Ada Lovelace",
        # Google believes an address only with ``hd`` or at Gmail
        # (``core.allow``), and an unverified address is not kept.
        "hd": "example.com",
        "email": "ada@example.com",
        "email_verified": True,
        **claims,
    }
    return {"id_token": unsigned_jwt(payload)}


async def begin(client: httpx.AsyncClient, *, return_to: str | None = None) -> httpx.Response:
    """Follow the sign-in button; the login cookie lands in the client's jar."""
    params = {"return_to": return_to} if return_to is not None else None
    return await client.get("/auth/login/google", params=params)


# The session route.


@asyncio_test
async def test_the_session_route_says_a_deployment_has_no_sign_in() -> None:
    async with serving(None) as client:
        answered = await client.get("/auth/session")

    assert answered.status_code == 200
    assert answered.json() == {
        "sign_in": False,
        "local_development": False,
        "public_url": None,
        "providers": [],
        "user": None,
    }
    assert answered.headers["cache-control"] == "no-store"


@asyncio_test
async def test_the_session_route_names_the_providers_and_nobody() -> None:
    async with serving(wired().sign_in) as client:
        answered = await client.get("/auth/session")

    assert answered.status_code == 200
    assert answered.json() == {
        "sign_in": True,
        "local_development": False,
        "public_url": PUBLIC_URL,
        "providers": [{"id": "google", "title": "Google"}],
        "user": None,
    }
    assert answered.headers["cache-control"] == "no-store"


@asyncio_test
async def test_the_session_route_shows_who_is_signed_in() -> None:
    deployment = wired()
    secret, user = await open_session(deployment)

    async with serving(deployment.sign_in) as client:
        answered = await client.get(
            "/auth/session", headers=cookie(session_cookie(secure=True), secret)
        )

    assert answered.json()["user"] == {
        "id": str(user.id),
        "name": "Ada Lovelace",
        "email": "ada@example.com",
        "provider": "google",
    }


@pytest.mark.parametrize("held", ["nonsense", "x" * 43, ""])
@asyncio_test
async def test_the_session_route_is_never_a_401(held: str) -> None:
    """Whatever the cookie holds, "nobody" is an answer and not a refusal."""
    async with serving(wired().sign_in) as client:
        answered = await client.get(
            "/auth/session", headers=cookie(session_cookie(secure=True), held)
        )

    assert answered.status_code == 200
    assert answered.json()["user"] is None


# Beginning a sign-in.


@asyncio_test
async def test_the_login_route_redirects_to_the_provider_and_keeps_the_state() -> None:
    deployment = wired()

    async with serving(deployment.sign_in) as client:
        answered = await begin(client)

    assert answered.status_code == 303
    location = answered.headers["location"]
    assert location.startswith(f"{GOOGLE.issuer}/authorize?")
    assert answered.headers["cache-control"] == "no-store"
    state = deployment.secrets.given[0]
    assert query_of(location, "state") == state
    assert query_of(location, "redirect_uri") == f"{PUBLIC_URL}/auth/callback/google"
    assert query_of(location, "code_challenge_method") == "S256"
    # The cookie holds the raw state; the store holds its SHA-256 alone.
    held, attributes = set_cookies(answered)["__Host-robinauts_login"]
    assert held == state
    assert f"max-age={LOGIN_SECONDS}" in attributes
    assert "httponly" in attributes
    assert "secure" in attributes
    assert "samesite=lax" in attributes
    assert "path=/" in attributes


@asyncio_test
async def test_a_loopback_deployment_sets_an_unprefixed_insecure_cookie() -> None:
    """``__Host-`` demands ``Secure``, which ``http`` cannot be: the plain name."""
    deployment = wired(public_url=LOOPBACK_URL)

    async with serving(deployment.sign_in, base_url=LOOPBACK_URL) as client:
        answered = await begin(client)

    set_by = set_cookies(answered)
    assert "__Host-robinauts_login" not in set_by
    assert "secure" not in set_by["robinauts_login"][1]
    assert "httponly" in set_by["robinauts_login"][1]
    assert login_cookie(secure=False) == "robinauts_login"


@pytest.mark.parametrize(
    "return_to",
    [
        "//evil.example.com/",
        "https://evil.example.com/",
        "/\\evil.example.com",
        "http:/evil.example.com",
        "/chat\r\nSet-Cookie: x=1",
        "chat/42",
    ],
)
@asyncio_test
async def test_a_return_target_that_is_not_ours_is_dropped(return_to: str) -> None:
    """The sign-in goes on; where it lands afterwards is this deployment.

    ``core.safe_return_to`` is what refuses these, and it is asked twice --
    when the sign-in is stored and when it is used -- so nothing here has to
    be trusted to have done it.
    """
    deployment = wired()

    async with serving(deployment.sign_in) as client:
        begun = await begin(client, return_to=return_to)
        landed = await complete(client, deployment)

    assert begun.status_code == 303
    assert landed.headers["location"] == f"{PUBLIC_URL}/ui/"


@asyncio_test
async def test_a_hash_route_is_where_the_person_comes_back_to() -> None:
    deployment = wired()

    async with serving(deployment.sign_in) as client:
        await begin(client, return_to="/#/chat/42")
        landed = await complete(client, deployment)

    assert landed.headers["location"] == f"{PUBLIC_URL}/ui/#/chat/42"


# Finishing one.


async def complete(
    client: httpx.AsyncClient,
    deployment: Wired,
    *,
    state: str | None = None,
    **query: str,
) -> httpx.Response:
    """The callback the provider sends the browser to, with a working code.

    The token endpoint answers with an ID token for this sign-in, unless the
    test has already said what it answers -- which is how the failures below
    script a token that must be refused.
    """
    if "google" not in deployment.provider.tokens:
        deployment.provider.answers(
            GOOGLE, token_for(deployment, nonce=deployment.secrets.given[1])
        )
    parameters = {"code": "an-authorization-code", **query}
    parameters["state"] = state if state is not None else deployment.secrets.given[0]
    return await client.get("/auth/callback/google", params=parameters)


@asyncio_test
async def test_a_finished_sign_in_lands_in_the_interface_with_a_session() -> None:
    deployment = wired()

    async with serving(deployment.sign_in) as client:
        await begin(client)
        landed = await complete(client, deployment)

    assert landed.status_code == 303
    assert landed.headers["location"] == f"{PUBLIC_URL}/ui/"
    assert landed.headers["cache-control"] == "no-store"
    set_by = set_cookies(landed)
    # The sign-in is over: its cookie goes, and the session's arrives.
    assert "max-age=0" in set_by["__Host-robinauts_login"][1]
    secret, session = set_by["__Host-robinauts_session"]
    assert f"max-age={SESSION_SECONDS}" in session
    assert "httponly" in session
    assert "secure" in session
    assert "samesite=lax" in session
    assert secret not in deployment.store.everything()
    assert len(deployment.store.sessions) == 1


@asyncio_test
async def test_the_session_route_then_shows_the_person() -> None:
    deployment = wired()

    async with serving(deployment.sign_in) as client:
        await begin(client)
        await complete(client, deployment)
        answered = await client.get("/auth/session")

    assert answered.json()["user"]["email"] == "ada@example.com"


def unavailable(deployment: Wired) -> None:
    deployment.provider.documents["google"] = ProviderUnavailableError("the provider is down")


def not_allowed(deployment: Wired) -> None:
    deployment.provider.answers(
        GOOGLE,
        token_for(deployment, nonce=deployment.secrets.given[1], email="mallory@example.org"),
    )


def wrong_nonce(deployment: Wired) -> None:
    deployment.provider.answers(GOOGLE, token_for(deployment, nonce="another-sign-in"))


FAILURES: dict[SignInErrorCode, dict[str, Any]] = {
    SignInErrorCode.UNKNOWN_PROVIDER: {"provider": "nope"},
    SignInErrorCode.PROVIDER_UNAVAILABLE: {"before_login": unavailable},
    SignInErrorCode.BUSY: {"max_pending_logins": 1, "logins": 2},
    SignInErrorCode.STATE_MISMATCH: {"state": "not-the-state-this-browser-began-with"},
    SignInErrorCode.EXPIRED: {"advance": LOGIN_SECONDS + 1},
    SignInErrorCode.PROVIDER_REFUSED: {"query": {"error": "access_denied"}},
    SignInErrorCode.NOT_ALLOWED: {"before_callback": not_allowed, "allow": ()},
    SignInErrorCode.INVALID_ID_TOKEN: {"before_callback": wrong_nonce},
}
"""One way to reach each of the eight fixed codes, through the routes."""


@pytest.mark.parametrize("code", list(SignInErrorCode))
@asyncio_test
async def test_every_sign_in_failure_ends_at_the_sign_in_page(
    code: SignInErrorCode,
) -> None:
    """One code, no words of the provider's, and the login cookie cleared."""
    how = FAILURES[code]
    allowed: Any = how.get("allow")
    deployment = wired(
        max_pending_logins=int(how.get("max_pending_logins", 10_000)),
        **({"allow": allowed} if allowed is not None else {}),
    )
    before_login: Callable[[Wired], None] | None = how.get("before_login")
    if before_login is not None:
        before_login(deployment)

    async with serving(deployment.sign_in) as client:
        for _ in range(int(how.get("logins", 1))):
            answered = await client.get(f"/auth/login/{how.get('provider', 'google')}")
        if answered.status_code == 303 and answered.headers["location"].startswith(GOOGLE.issuer):
            if how.get("advance"):
                deployment.clock.advance(float(how["advance"]))
            before_callback: Callable[[Wired], None] | None = how.get("before_callback")
            if before_callback is not None:
                before_callback(deployment)
            answered = await complete(
                client,
                deployment,
                state=how.get("state"),
                **how.get("query", {}),
            )

    assert answered.status_code == 303
    assert answered.headers["location"] == f"{PUBLIC_URL}{SIGN_IN_PAGE}{code.value}"
    assert answered.headers["cache-control"] == "no-store"
    assert "max-age=0" in set_cookies(answered)["__Host-robinauts_login"][1]
    assert "__Host-robinauts_session" not in set_cookies(answered)
    assert deployment.store.sessions == {}


@asyncio_test
async def test_a_callback_with_no_cookie_is_a_state_mismatch() -> None:
    """The query alone proves nothing: the state has to be this browser's."""
    deployment = wired()

    async with serving(deployment.sign_in) as client:
        await begin(client)
        client.cookies.clear()
        landed = await complete(client, deployment)

    assert landed.headers["location"] == (
        f"{PUBLIC_URL}{SIGN_IN_PAGE}{SignInErrorCode.STATE_MISMATCH.value}"
    )


@asyncio_test
async def test_a_sign_in_cannot_be_finished_twice() -> None:
    deployment = wired()

    async with serving(deployment.sign_in) as client:
        await begin(client)
        state = deployment.secrets.given[0]
        first = await complete(client, deployment)
        # The session cookie is in the jar now; the login cookie is not, so
        # the state is sent back by hand.
        again = await client.get(
            "/auth/callback/google",
            params={"code": "another", "state": state},
            headers=cookie(login_cookie(secure=True), state),
        )

    assert first.headers["location"] == f"{PUBLIC_URL}/ui/"
    assert again.headers["location"] == (
        f"{PUBLIC_URL}{SIGN_IN_PAGE}{SignInErrorCode.EXPIRED.value}"
    )


# Signing out.


@asyncio_test
async def test_signing_out_ends_the_session_and_clears_the_cookie() -> None:
    deployment = wired()

    async with serving(deployment.sign_in) as client:
        await begin(client)
        await complete(client, deployment)
        out = await client.post("/auth/logout", headers={**JSON, "origin": PUBLIC_URL})
        after = await client.get("/auth/session")

    assert out.status_code == 204
    assert out.content == b""
    assert "max-age=0" in set_cookies(out)["__Host-robinauts_session"][1]
    assert deployment.store.sessions == {}
    assert after.json()["user"] is None


@asyncio_test
async def test_signing_out_without_a_session_is_the_same_answer() -> None:
    async with serving(wired().sign_in) as client:
        first = await client.post("/auth/logout", headers=JSON)
        second = await client.post("/auth/logout", headers=JSON)

    assert (first.status_code, second.status_code) == (204, 204)


@asyncio_test
async def test_the_sign_in_routes_are_not_found_without_a_sign_in() -> None:
    async with serving(None) as client:
        login = await client.get("/auth/login/google")
        callback = await client.get("/auth/callback/google")
        out = await client.post("/auth/logout", headers=JSON)

    assert (login.status_code, callback.status_code) == (404, 404)
    assert login.json()["error"] == "UnknownProviderError"
    assert out.status_code == 204


# What else the application serves.


@asyncio_test
async def test_health_is_public_and_holds_nothing() -> None:
    async with serving(wired().sign_in) as client:
        answered = await client.get("/health")

    assert (answered.status_code, answered.json()) == (200, {"status": "ok"})


@asyncio_test
async def test_the_documentation_pages_are_not_served_and_the_document_is() -> None:
    """``/docs`` and ``/redoc`` load scripts from a CDN, which is forbidden."""
    async with serving(wired().sign_in) as client:
        docs = await client.get("/docs")
        redoc = await client.get("/redoc")
        document = await client.get("/openapi.json")

    assert (docs.status_code, redoc.status_code) == (404, 404)
    assert document.status_code == 200
    assert document.json()["info"]["title"] == "Robinauts"


# What the routes are careful not to hand back, or to leave behind.


@pytest.mark.parametrize(
    "named",
    [
        "Google",
        "a" * 200,
        "../../etc/passwd",
        "<script>alert(1)</script>",
        "google\nX-Injected: yes",
        "",
    ],
)
@asyncio_test
async def test_a_provider_id_that_is_not_one_is_refused_without_being_repeated(
    named: str, caplog: pytest.LogCaptureFixture
) -> None:
    """A path parameter is whatever the link said, so it goes nowhere unescaped.

    Not into the body, where it would be reflected back at whoever wrote the
    link; and into a log line only through ``robinauts.legacy.api.logs.shown``, since
    a newline of theirs written out as it came would be a line of ours.
    """
    deployment = wired()

    with caplog.at_level(logging.WARNING):
        async with serving(deployment.sign_in) as client:
            login = await client.get(f"/auth/login/{quote(named, safe='')}")
            callback = await client.get(f"/auth/callback/{quote(named, safe='')}")

    for answered in (login, callback):
        if answered.status_code == 404:
            # The router never matched it: a path parameter cannot hold a
            # slash, so it was not this route's to refuse.
            assert answered.json()["detail"] == "not found"
            continue
        # These two are navigations, so a browser is sent where every other
        # failed sign-in goes rather than shown a JSON body as a blank page.
        assert answered.status_code == 303
        assert answered.headers["location"] == (
            f"{PUBLIC_URL}{SIGN_IN_PAGE}{SignInErrorCode.UNKNOWN_PROVIDER.value}"
        )
        assert answered.headers["cache-control"] == "no-store"
    if named:
        # Never in an answer: that would be reflecting whatever the link said
        # back at whoever followed it.
        assert named not in login.text
        assert named not in callback.text
    # In the log, and only escaped: no record it reached can end a line or
    # start one, whatever the id held.
    assert all("\n" not in record.getMessage() for record in caplog.records)
    assert deployment.provider.discoveries == []


@asyncio_test
async def test_a_well_formed_id_that_is_not_configured_reaches_the_sign_in_page() -> None:
    """Spelt as an id, and simply not there: a person, not an attack.

    They pressed a button on a page that is out of date, so they are sent
    where every other failed sign-in goes, with the code for it. The id is
    only in the log, and it got there having been checked for shape: forty
    characters of lower-case letters, digits, ``_`` and ``-``.
    """
    async with serving(wired().sign_in) as client:
        answered = await client.get("/auth/login/okta")

    assert answered.status_code == 303
    assert answered.headers["location"] == (
        f"{PUBLIC_URL}{SIGN_IN_PAGE}{SignInErrorCode.UNKNOWN_PROVIDER.value}"
    )


class BrokenStore(MemoryCredentialStore):
    """A store that fails in the middle of a callback, as a database may."""

    async def take_pending_login(self, key: str) -> Any:
        raise RuntimeError("the connection to the database went away")


@asyncio_test
async def test_a_misshapen_id_on_the_callback_clears_the_login_cookie() -> None:
    """It is a sign-in ending, so the state cookie it began with goes with it."""
    deployment = wired()

    async with serving(deployment.sign_in) as client:
        await begin(client)
        landed = await client.get("/auth/callback/NOT-AN-ID", params={"code": "c", "state": "s"})

    assert landed.status_code == 303
    assert landed.headers["location"] == (
        f"{PUBLIC_URL}{SIGN_IN_PAGE}{SignInErrorCode.UNKNOWN_PROVIDER.value}"
    )
    assert "max-age=0" in set_cookies(landed)["__Host-robinauts_login"][1]
    assert "__Host-robinauts_session" not in set_cookies(landed)


@asyncio_test
async def test_a_callback_that_breaks_still_clears_the_login_cookie(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """However a callback ends, that sign-in is over.

    A state cookie left in the browser is a sign-in waiting to be finished by
    the next callback that arrives -- and the next callback is whatever a link
    sends the browser to.
    """
    deployment = Wired(sign_in_config(), store=BrokenStore())
    deployment.provider.publishes(GOOGLE)

    with caplog.at_level(logging.ERROR):
        async with serving(deployment.sign_in) as client:
            await begin(client)
            broken = await client.get(
                "/auth/callback/google",
                params={"code": "a-code", "state": deployment.secrets.given[0]},
            )

    assert broken.status_code == 500
    assert broken.json() == {"error": "InternalError", "detail": "the request could not be served"}
    assert "max-age=0" in set_cookies(broken)["__Host-robinauts_login"][1]
    assert broken.headers["cache-control"] == "no-store"
    assert "the connection to the database went away" in caplog.text
