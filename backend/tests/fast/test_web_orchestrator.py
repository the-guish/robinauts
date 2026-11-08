# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The bearer of the orchestrator the server starts beside itself: the ``rbo_`` secret minted at
the start reaches the API as the local development mode's user, or where sign-in is on as the
orchestrator's own user; it never mints a credential, and any other ``rbo_`` bearer is 401."""

from __future__ import annotations

import httpx
from echo_controller import CONFIG

from robinauts.cli.composition import compose
from robinauts.shared_domain.domain import StorageConfig, StorageKind
from robinauts.web.app import LOCAL_IDENTITY, ORCHESTRATOR_IDENTITY
from robinauts.web.sign_in import (
    AllowEntry,
    Matcher,
    ProviderConfig,
    SecretKind,
    SignInConfig,
    secret_of_kind,
)
from util.aio import asyncio_test
from util.app import running

PUBLIC_URL = "https://robinauts.example.com"
SIGN_IN = SignInConfig(
    public_url=PUBLIC_URL,
    providers={
        "okta": ProviderConfig(
            id="okta",
            title="Okta",
            issuer="https://example.okta.com/oauth2/default",
            client_id="robinauts",
            client_secret_env="ROBINAUTS_OKTA_SECRET",
        )
    },
    allow=(AllowEntry("okta", Matcher.EVERYONE, None),),
)
TURN = {"agent_id": "echo", "text": "guess which number I'm thinking"}


def bearer(secret: str) -> dict[str, str]:
    return {"authorization": f"Bearer {secret}"}


@asyncio_test
async def test_in_the_local_mode_the_orchestrator_starts_sessions_of_the_local_user() -> None:
    secret = secret_of_kind(SecretKind.ORCHESTRATOR)
    composed = compose(CONFIG, storage=StorageConfig(StorageKind.IN_PROCESS), secret_for={}.get)
    async with running(
        composed, sign_in=None, secret_for={}.get, orchestrator_secret=secret
    ) as app:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as http:
            started = await http.post("/api/turns", json=TURN, headers=bearer(secret))
            assert started.status_code == 200
            conversation = started.headers["x-robinauts-conversation-id"]
            # The browser of the local mode, which sends no bearer, sees it as its own.
            mine = (await http.get("/api/conversations")).json()["items"]
            assert [c["id"] for c in mine] == [conversation]
            local = await composed.controller.ensure_user(LOCAL_IDENTITY)
            session = (await http.get("/auth/session")).json()
            assert session["user"]["id"] == str(local.id)

            wrong = await http.get("/api/agents", headers=bearer(secret + "x"))
            assert wrong.status_code == 401
            assert wrong.json()["error"] == "Unauthorized"
            minting = await http.post("/auth/tokens", json={"name": "x"}, headers=bearer(secret))
            assert minting.status_code == 403


@asyncio_test
async def test_with_sign_in_the_orchestrator_is_a_user_of_its_own_and_unknown_without_one() -> None:
    secret = secret_of_kind(SecretKind.ORCHESTRATOR)
    composed = compose(CONFIG, storage=StorageConfig(StorageKind.IN_PROCESS), secret_for={}.get)
    async with running(
        composed, sign_in=SIGN_IN, secret_for={}.get, orchestrator_secret=secret
    ) as app:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=PUBLIC_URL) as http:
            started = await http.post("/api/turns", json=TURN, headers=bearer(secret))
            assert started.status_code == 200
            orchestrator = await composed.controller.ensure_user(ORCHESTRATOR_IDENTITY)
            page = await composed.controller.list_sessions(orchestrator, limit=10)
            ids = [str(s.id) for s in page.sessions]
            assert ids == [started.headers["x-robinauts-conversation-id"]]
            assert (await http.get("/api/agents")).status_code == 401

    async with running(composed, sign_in=SIGN_IN, secret_for={}.get) as app:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=PUBLIC_URL) as http:
            assert (await http.get("/api/agents", headers=bearer(secret))).status_code == 401
