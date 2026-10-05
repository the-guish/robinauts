# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""API tokens over the web app on the memory credentials: minted by a person signed in with a
session cookie, shown once, listed, sent as a bearer with no cookie and no ``Origin``, and
refused once revoked or past their expiry. No provider is asked: the session is added to the
credentials directly."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import httpx
from echo_controller import CONFIG

from robinauts.controller.composition import Composed, compose
from robinauts.controller.contract.domain import (
    ApiToken,
    Identity,
    StorageConfig,
    StorageKind,
    User,
    UserSession,
)
from robinauts.web.app import LOCAL_IDENTITY, TOKEN_LIFE, create_app
from robinauts.web.cookies import Cookies
from robinauts.web.sign_in import (
    AllowEntry,
    Matcher,
    ProviderConfig,
    SignInConfig,
    random_secret,
    secret_hash,
)
from util.aio import asyncio_test

PUBLIC_URL = "https://robinauts.example.com"
OKTA = ProviderConfig(
    id="okta",
    title="Okta",
    issuer="https://example.okta.com/oauth2/default",
    client_id="robinauts",
    client_secret_env="ROBINAUTS_OKTA_SECRET",
)
SIGN_IN = SignInConfig(
    public_url=PUBLIC_URL,
    providers={"okta": OKTA},
    allow=(AllowEntry("okta", Matcher.EVERYONE, None),),
)
ADA = Identity("okta", "ada", name="Ada Lovelace", email="ada@example.com")
TURN = {"agent_id": "echo", "text": "hello"}


@asynccontextmanager
async def signed_in() -> AsyncIterator[tuple[httpx.AsyncClient, Composed, User, dict[str, str]]]:
    """The app, a client, Ada, and the headers of her browser: her session cookie and the
    ``Origin`` a write with it needs."""
    composed = compose(CONFIG, storage=StorageConfig(StorageKind.IN_MEMORY), secret_for={}.get)
    app = create_app(composed, sign_in=SIGN_IN, secret_for={}.get)
    async with app.router.lifespan_context(app):
        ada = await composed.controller.ensure_user(ADA)
        secret = random_secret()
        now = datetime.now(UTC)
        session = UserSession(
            uuid.uuid4(), ada.id, secret_hash(secret), now, now + timedelta(hours=1)
        )
        await composed.credentials.add_user_session(session)
        browser = {"cookie": f"{Cookies(PUBLIC_URL).session}={secret}", "origin": PUBLIC_URL}
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=PUBLIC_URL) as http:
            yield http, composed, ada, browser


def bearer(secret: str) -> dict[str, str]:
    return {"authorization": f"Bearer {secret}"}


async def token_of(composed: Composed, user: User, expires_at: datetime) -> str:
    """A token added to the credentials directly; its secret."""
    secret = random_secret()
    token = ApiToken(
        uuid.uuid4(), user.id, "added", secret_hash(secret), expires_at - TOKEN_LIFE, expires_at
    )
    await composed.credentials.add_api_token(token)
    return secret


@asyncio_test
async def test_a_token_is_shown_once_reaches_the_api_and_is_refused_once_revoked() -> None:
    async with signed_in() as (http, composed, ada, browser):
        minted = await http.post("/auth/tokens", json={"name": "laptop"}, headers=browser)
        assert minted.status_code == 201
        token = minted.json()
        assert token.keys() == {"id", "name", "created_at", "expires_at", "secret"}
        assert token["name"] == "laptop"
        lived = datetime.fromisoformat(token["expires_at"]) - datetime.fromisoformat(
            token["created_at"]
        )
        assert lived == timedelta(days=90)
        listed = (await http.get("/auth/tokens", headers=browser)).json()
        assert listed == {"items": [{k: v for k, v in token.items() if k != "secret"}]}

        # The token reaches the API with no cookie and no Origin.
        started = await http.post("/api/turns", json=TURN, headers=browser)
        conversation_id = started.headers["x-robinauts-conversation-id"]
        key = bearer(token["secret"])
        mine = (await http.get("/api/conversations", headers=key)).json()["items"]
        assert [c["id"] for c in mine] == [conversation_id]
        renamed = await http.patch(
            f"/api/conversations/{conversation_id}", json={"title": "By token"}, headers=key
        )
        assert renamed.status_code == 200
        assert renamed.json()["title"] == "By token"
        assert (await http.post("/api/turns", json=TURN, headers=key)).status_code == 200

        # Somebody else's token is not found, and keeps working.
        grace = await composed.controller.ensure_user(Identity("okta", "grace", name="Grace"))
        hers = await token_of(composed, grace, datetime.now(UTC) + TOKEN_LIFE)
        listed = (await http.get("/auth/tokens", headers=bearer(hers))).json()["items"]
        refused = await http.delete(f"/auth/tokens/{listed[0]['id']}", headers=browser)
        assert refused.status_code == 404
        assert refused.json()["error"] == "NotFound"
        assert (await http.get("/api/conversations", headers=bearer(hers))).status_code == 200

        # Revoked, it is listed no more; it is refused like an unknown, an expired or a local
        # user's token.
        revoked = await http.delete(f"/auth/tokens/{token['id']}", headers=browser)
        assert revoked.status_code == 204
        assert (await http.get("/auth/tokens", headers=browser)).json() == {"items": []}
        local = await composed.controller.ensure_user(LOCAL_IDENTITY)
        for secret in (
            token["secret"],
            random_secret(),
            await token_of(composed, ada, datetime.now(UTC) - timedelta(seconds=1)),
            await token_of(composed, local, datetime.now(UTC) + TOKEN_LIFE),
        ):
            refused = await http.get("/api/conversations", headers=bearer(secret))
            assert refused.status_code == 401
            assert refused.json()["error"] == "Unauthorized"
