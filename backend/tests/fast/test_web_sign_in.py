# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Signing in through the stand-in provider, over the web app on the in-process credentials: the
login cookie, the callback, the session cookie, who is asking, the ``Origin`` check and
signing out; a refusal of the allow list ending every credential of that identity; and the
local development mode beside it. Marked ``io``: the stand-in listens on
a port the operating system picks."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from robinauts.controller.composition import compose
from robinauts.controller.contract.domain import (
    AgentConfig,
    Config,
    ModelConfig,
    ProviderKind,
    StorageConfig,
    StorageKind,
)
from robinauts.controller.contract.domain import ProviderConfig as ModelProvider
from robinauts.web.app import create_app
from robinauts.web.sign_in import AllowEntry, Matcher, ProviderConfig, SignInConfig
from util.aio import asyncio_test
from util.standin import Misbehaviour, StandInProvider, redirect_from

pytestmark = pytest.mark.io

PUBLIC_URL = "http://127.0.0.1:8000"
SECRET_ENV = "ROBINAUTS_STAND_IN_SECRET"
ECHO = Config(
    providers={"echo": ModelProvider("echo", ProviderKind.ANTHROPIC, "ECHO_API_KEY")},
    models={"echo": ModelConfig("echo", provider="echo", name="echo", title="Echo")},
    agents={
        "echo": AgentConfig("echo", title="Echo", system_prompt="", model="echo", engine="echo")
    },
)
TURN = {"agent_id": "echo", "text": "hello"}


@pytest.fixture
def stand_in() -> Iterator[StandInProvider]:
    with StandInProvider() as provider:
        provider.person["groups"] = ["robinauts-users"]
        yield provider


@asynccontextmanager
async def browser(stand_in: StandInProvider | None) -> AsyncIterator[httpx.AsyncClient]:
    """The app signing in through the stand-in, or with ``None`` the local development mode,
    and a client that keeps its cookies and follows no redirect."""
    sign_in = None
    secret_for = {}.get
    if stand_in is not None:
        okta = ProviderConfig(
            id="okta",
            title="Okta",
            issuer=stand_in.issuer,
            client_id=stand_in.client_id,
            client_secret_env=SECRET_ENV,
            scopes=("openid", "email", "profile", "groups"),
            groups_claim="groups",
        )
        allow = (AllowEntry("okta", Matcher.GROUP, "robinauts-users"),)
        sign_in = SignInConfig(public_url=PUBLIC_URL, providers={"okta": okta}, allow=allow)
        secret_for = {SECRET_ENV: stand_in.client_secret}.get
    composed = compose(ECHO, storage=StorageConfig(StorageKind.IN_PROCESS), secret_for=secret_for)
    app = create_app(
        composed,
        sign_in=sign_in,
        secret_for=secret_for,
    )
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=PUBLIC_URL) as http:
            yield http


async def signed_in(http: httpx.AsyncClient) -> httpx.Response:
    """Begin, follow the provider's redirect by hand, and answer the callback."""
    begun = await http.get("/auth/login/okta", params={"return_to": "/#/chats"})
    return await http.get(await redirect_from(begun.headers["location"]))


@asyncio_test
async def test_a_person_signs_in_writes_from_public_url_only_and_signs_out(
    stand_in: StandInProvider,
) -> None:
    async with browser(stand_in) as http:
        nobody = await http.get("/api/conversations")
        assert nobody.status_code == 401
        assert nobody.json()["error"] == "Unauthorized"

        begun = await http.get("/auth/login/okta", params={"return_to": "/#/chats"})
        assert begun.status_code == 302
        assert begun.headers["location"].startswith(f"{stand_in.issuer}/authorize?")
        login = begun.headers["set-cookie"]
        assert login.startswith("robinauts_login=")
        assert "HttpOnly" in login
        assert "Max-Age=600" in login
        assert "SameSite=lax" in login
        assert "Secure" not in login

        callback = await redirect_from(begun.headers["location"])
        assert callback.startswith(f"{PUBLIC_URL}/auth/callback/okta?")
        assert set(parse_qs(urlsplit(callback).query)) == {"code", "state"}
        landed = await http.get(callback)
        assert landed.status_code == 302
        assert landed.headers["location"] == f"{PUBLIC_URL}/#/chats"
        session_cookie, login_cleared = landed.headers.get_list("set-cookie")
        assert session_cookie.startswith("robinauts_session=")
        assert "Max-Age=43200" in session_cookie
        assert login_cleared.startswith('robinauts_login="";')

        session = (await http.get("/auth/session")).json()
        assert session["sign_in"] is True
        assert session["local_development"] is False
        assert session["public_url"] == PUBLIC_URL
        assert session["providers"] == [{"id": "okta", "title": "Okta"}]
        assert session["user"]["provider"] == "okta"
        assert session["user"]["name"] == "Ada Lovelace"
        assert session["user"]["email"] == "ada@example.com"
        assert (await http.get("/api/conversations")).status_code == 200

        # A write with the session cookie must come from public_url.
        refused = await http.post("/api/turns", json=TURN)
        assert refused.status_code == 403
        assert refused.json()["error"] == "Forbidden"
        elsewhere = await http.post("/api/turns", json=TURN, headers={"origin": "http://evil"})
        assert elsewhere.status_code == 403
        taken = await http.post("/api/turns", json=TURN, headers={"origin": PUBLIC_URL})
        assert taken.status_code == 200
        assert len((await http.get("/api/conversations")).json()["items"]) == 1

        # Signing out ends the session and clears the cookie.
        secret = http.cookies["robinauts_session"]

        out = await http.post("/auth/logout", headers={"origin": PUBLIC_URL})
        assert out.status_code == 204
        assert out.headers["set-cookie"].startswith('robinauts_session="";')
        assert "robinauts_session" not in http.cookies
        assert (await http.get("/api/conversations")).status_code == 401
        kept = await http.get(
            "/api/conversations", headers={"cookie": f"robinauts_session={secret}"}
        )
        assert kept.status_code == 401
        assert (await http.get("/auth/session")).json()["user"] is None


@asyncio_test
async def test_the_local_mode_answers_an_ip_or_localhost_and_writes_from_that_host_only() -> None:
    async with browser(None) as http:
        assert (await http.get("/api/conversations")).status_code == 200

        # A rebound name is refused, reads included.
        for host in (
            "evil.example",
            "evil.example:8000",
            "localhost.evil.example",
            "",
            "a@1.2.3.4",
        ):
            rebound = await http.get("/api/conversations", headers={"host": host})
            assert rebound.status_code == 403, host
            assert rebound.json()["error"] == "Forbidden"
        rebound = await http.post(
            "/api/turns",
            json=TURN,
            headers={"host": "evil.example", "origin": "http://evil.example"},
        )
        assert rebound.status_code == 403

        # A page elsewhere cannot write; this host, a phone through the LAN, or no Origin can.
        elsewhere = await http.post("/api/turns", json=TURN, headers={"origin": "http://evil"})
        assert elsewhere.status_code == 403
        assert (await http.post("/api/turns", json=TURN, headers={"origin": PUBLIC_URL})).is_success
        phone = {"host": "192.168.1.10:5173", "origin": "http://192.168.1.10:5173"}
        assert (await http.post("/api/turns", json=TURN, headers=phone)).is_success
        ipv6 = {"host": "[::1]:5173", "origin": "http://[::1]:5173"}
        assert (await http.post("/api/turns", json=TURN, headers=ipv6)).is_success
        local = {"host": "localhost:5173", "origin": "http://localhost:5173"}
        assert (await http.post("/api/turns", json=TURN, headers=local)).is_success
        assert (await http.post("/api/turns", json=TURN)).is_success
        assert len((await http.get("/api/conversations")).json()["items"]) == 5


@asyncio_test
async def test_a_person_not_on_the_allow_list_lands_on_the_sign_in_page(
    stand_in: StandInProvider,
) -> None:
    stand_in.person["groups"] = ["somebody-else"]
    async with browser(stand_in) as http:
        landed = await signed_in(http)

        assert landed.status_code == 302
        assert landed.headers["location"] == "/ui/#/sign-in?error=not_allowed"
        assert "robinauts_session" not in http.cookies
        assert (await http.get("/api/conversations")).status_code == 401


@asyncio_test
async def test_a_callback_whose_state_is_not_the_login_cookies_is_a_state_mismatch(
    stand_in: StandInProvider,
) -> None:
    async with browser(stand_in) as http:
        first = await http.get("/auth/login/okta")
        callback = await redirect_from(first.headers["location"])
        # A second sign-in begun in this browser replaces the login cookie.
        await http.get("/auth/login/okta")
        landed = await http.get(callback)

        assert landed.headers["location"] == "/ui/#/sign-in?error=state_mismatch"
        assert (await http.get("/api/conversations")).status_code == 401


@asyncio_test
async def test_a_refusal_of_the_allow_list_ends_every_credential_and_a_provider_error_none(
    stand_in: StandInProvider,
) -> None:
    async with browser(stand_in) as http:
        await signed_in(http)
        session = (await http.get("/auth/session")).json()
        assert session["user"] is not None
        token = await http.post("/auth/tokens", json={"name": "ci"}, headers={"origin": PUBLIC_URL})
        assert token.status_code == 201
        key = {"authorization": f"Bearer {token.json()['secret']}"}
        cookie = {"cookie": f"robinauts_session={http.cookies['robinauts_session']}"}

        # A provider that fails, at the token endpoint, wipes nothing.
        stand_in.token_misbehaves(Misbehaviour.server_error())
        failed = await signed_in(http)
        assert failed.headers["location"] == "/ui/#/sign-in?error=provider_unavailable"
        assert (await http.get("/api/conversations", headers=cookie)).status_code == 200
        assert (await http.get("/api/conversations", headers=key)).status_code == 200

        # The provider vouches for her, and the allow list no longer lets her in.
        stand_in.person["groups"] = ["somebody-else"]
        refused = await signed_in(http)
        assert refused.headers["location"] == "/ui/#/sign-in?error=not_allowed"
        assert (await http.get("/api/conversations", headers=cookie)).status_code == 401
        assert (await http.get("/api/conversations", headers=key)).status_code == 401
