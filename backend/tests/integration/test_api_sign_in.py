# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Signing in through the routes, browser-shaped, against a provider on a socket.

Everything but the database: the real application, the real HTTP adapter and
a real OpenID Connect provider listening on loopback, with the in-memory store
standing in for PostgreSQL. What the unit tests prove separately -- the
cookies, the redirects, the codes -- this proves fits together, in the order a
browser does it: the button, the provider, the callback, the session, the
sign-out.

The one part that is not ours is the browser, so the redirect from the
authorization endpoint is followed by hand (``standin.redirect_from``) and the
``Location`` it gives back is handed to the callback route, query and all.

Marked ``io``: it opens sockets.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import httpx
import pytest

from aio import asyncio_test
from fakes import MemoryCredentialStore
from robinauts.legacy.adapters import HttpIdentityProvider, OsSecretSource, SystemClock
from robinauts.legacy.api import SIGN_IN_PAGE, create_api
from robinauts.legacy.application import SignIn
from robinauts.legacy.core import secret_hash
from robinauts.legacy.domain import (
    AllowEntry,
    Matcher,
    ProviderConfig,
    SignInConfig,
    SignInErrorCode,
)
from standin import Misbehaviour, StandInProvider, redirect_from

pytestmark = pytest.mark.io

PUBLIC_URL = "https://robinauts.example.com"
SECRET_VARIABLE = "ROBINAUTS_STAND_IN_SECRET"
JSON = {"content-type": "application/json"}


@pytest.fixture
def stand_in() -> Iterator[StandInProvider]:
    with StandInProvider() as provider:
        yield provider


def configuration(stand_in: StandInProvider) -> SignInConfig:
    return SignInConfig(
        public_url=PUBLIC_URL,
        providers={
            "okta": ProviderConfig(
                id="okta",
                title="Okta",
                issuer=stand_in.issuer,
                client_id=stand_in.client_id,
                client_secret_env=SECRET_VARIABLE,
                scopes=("openid", "email", "profile", "groups"),
                groups_claim="groups",
            )
        },
        allow=(AllowEntry("okta", Matcher.EMAIL, "ada@example.com"),),
    )


class Browser:
    """A client on the application, and what a browser does between requests."""

    def __init__(self, client: httpx.AsyncClient, store: MemoryCredentialStore) -> None:
        self.client = client
        self.store = store

    async def sign_in(self, *, return_to: str | None = None) -> httpx.Response:
        """Press the button, follow the provider's redirect, come back."""
        begun = await self.client.get(
            "/auth/login/okta",
            params={"return_to": return_to} if return_to is not None else None,
        )
        assert begun.status_code == 303, begun.text
        back = await redirect_from(begun.headers["location"])
        assert back.startswith(f"{PUBLIC_URL}/auth/callback/okta")
        return await self.client.get(f"/auth/callback/okta?{urlsplit(back).query}")

    async def session(self) -> dict:
        answered = await self.client.get("/auth/session")
        assert answered.status_code == 200
        return answered.json()


@asynccontextmanager
async def deployed(stand_in: StandInProvider) -> AsyncIterator[Browser]:
    """The whole inbound side over the real adapter, closed however it ends."""
    store = MemoryCredentialStore()
    adapter = HttpIdentityProvider(
        secret_for={SECRET_VARIABLE: stand_in.client_secret}.get, trust_env=False
    )
    sign_in = SignIn(
        configuration(stand_in),
        credentials=store,
        provider=adapter,
        clock=SystemClock(),
        secrets=OsSecretSource(),
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_api(sign_in)),
            base_url=PUBLIC_URL,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            yield Browser(client, store)
    finally:
        await adapter.aclose()


@asyncio_test
async def test_a_whole_sign_in_ends_with_a_session_the_routes_recognise(
    stand_in: StandInProvider,
) -> None:
    async with deployed(stand_in) as browser:
        before = await browser.session()
        landed = await browser.sign_in(return_to="/#/chat/42")
        after = await browser.session()

    assert before == {
        "sign_in": True,
        "local_development": False,
        "public_url": PUBLIC_URL,
        "providers": [{"id": "okta", "title": "Okta"}],
        "user": None,
    }
    assert landed.status_code == 303
    assert landed.headers["location"] == f"{PUBLIC_URL}/ui/#/chat/42"
    assert after["user"] == {
        "id": after["user"]["id"],
        "name": "Ada Lovelace",
        "email": "ada@example.com",
        "provider": "okta",
    }


@asyncio_test
async def test_signing_out_ends_it_and_the_session_route_says_so(
    stand_in: StandInProvider,
) -> None:
    async with deployed(stand_in) as browser:
        await browser.sign_in()
        out = await browser.client.post("/auth/logout", headers={**JSON, "origin": PUBLIC_URL})
        after = await browser.session()
        again = await browser.client.post("/auth/logout", headers={**JSON, "origin": PUBLIC_URL})

    assert (out.status_code, again.status_code) == (204, 204)
    assert after["user"] is None
    assert browser.store.sessions == {}


@asyncio_test
async def test_the_cookie_the_browser_is_given_is_not_in_the_store(
    stand_in: StandInProvider,
) -> None:
    """A stolen table hands nobody a session: what is kept is the SHA-256."""
    async with deployed(stand_in) as browser:
        landed = await browser.sign_in()
        held = browser.client.cookies["__Host-robinauts_session"]
        written = browser.store.everything()

    assert landed.status_code == 303
    assert held not in written
    assert secret_hash(held) in written
    assert stand_in.client_secret not in written


@asyncio_test
async def test_the_provider_was_asked_exactly_what_the_flow_asks_for(
    stand_in: StandInProvider,
) -> None:
    async with deployed(stand_in) as browser:
        await browser.sign_in()

    asked = stand_in.authorized[-1]
    assert asked["response_type"] == "code"
    assert asked["redirect_uri"] == f"{PUBLIC_URL}/auth/callback/okta"
    assert asked["code_challenge_method"] == "S256"
    posted = stand_in.exchanged[-1]
    # Only the challenge went to the browser; the verifier proves, at the
    # token endpoint, that this is the browser that asked.
    assert posted["code_verifier"] not in asked.values()


@asyncio_test
async def test_somebody_the_allow_list_does_not_have_reaches_the_sign_in_page(
    stand_in: StandInProvider,
) -> None:
    stand_in.person.update({"sub": "999", "email": "mallory@example.org"})

    async with deployed(stand_in) as browser:
        landed = await browser.sign_in()
        after = await browser.session()

    assert landed.headers["location"] == (
        f"{PUBLIC_URL}{SIGN_IN_PAGE}{SignInErrorCode.NOT_ALLOWED.value}"
    )
    assert "__Host-robinauts_session" not in browser.client.cookies
    assert after["user"] is None
    assert browser.store.users == []


@asyncio_test
async def test_a_provider_that_is_down_reaches_the_sign_in_page(
    stand_in: StandInProvider,
) -> None:
    stand_in.discovery_misbehaves(Misbehaviour.server_error())

    async with deployed(stand_in) as browser:
        begun = await browser.client.get("/auth/login/okta")

    assert begun.status_code == 303
    assert begun.headers["location"] == (
        f"{PUBLIC_URL}{SIGN_IN_PAGE}{SignInErrorCode.PROVIDER_UNAVAILABLE.value}"
    )
    assert "__Host-robinauts_login" not in browser.client.cookies


@asyncio_test
async def test_a_refused_code_reaches_the_sign_in_page(
    stand_in: StandInProvider,
) -> None:
    async with deployed(stand_in) as browser:
        begun = await browser.client.get("/auth/login/okta")
        back = await redirect_from(begun.headers["location"])
        stand_in.token_misbehaves(Misbehaviour.oauth_error("invalid_grant"))
        landed = await browser.client.get(f"/auth/callback/okta?{urlsplit(back).query}")

    assert landed.headers["location"] == (
        f"{PUBLIC_URL}{SIGN_IN_PAGE}{SignInErrorCode.PROVIDER_REFUSED.value}"
    )
    assert browser.store.sessions == {}
