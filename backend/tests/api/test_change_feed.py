# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""``GET /api/changes`` at its edges (``docs/specs/wire.md``, "The change feed"): no ``after``,
``after`` at, above and below the version, the timeout, the refusals, and a change of another
user, which neither wakes the wait nor shows in it.

The first test is on the shared server, whose local development mode has one user. The second
needs two users, so it starts an app of its own with sign-in, over a schema of its own.
"""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta

import httpx

from robinauts.cli.composition import compose
from robinauts.controller.contract.domain import ApiToken, Identity, User
from robinauts.shared_domain.domain import (
    AgentConfig,
    Config,
    ModelConfig,
    ProviderConfig,
    ProviderKind,
    StorageConfig,
    StorageKind,
)
from robinauts.web.sign_in import AllowEntry, Matcher, SignInConfig, random_secret, secret_hash
from robinauts.web.sign_in import ProviderConfig as SignInProvider
from util.aio import asyncio_test
from util.app import running
from util.controller_db import requires_postgres, temporary_schema, url

pytestmark = requires_postgres


def version(api: httpx.Client, **query: float) -> int:
    answered = api.get("/api/changes", params=query)
    assert answered.status_code == 200, answered.text
    assert answered.json().keys() == {"version"}
    return answered.json()["version"]


def test_the_feed_answers_at_once_waits_while_unchanged_and_wakes_on_a_change(
    api: httpx.Client,
) -> None:
    started = api.post("/api/turns", json={"agent_id": "pydantic_ai", "text": "feed"})
    assert started.status_code == 200, started.text
    cid = started.headers["x-robinauts-conversation-id"]
    now = version(api)
    assert now > 0

    # At the version, it waits for the timeout and answers it unchanged.
    began = time.monotonic()
    assert version(api, after=now, timeout=0.5) == now
    assert time.monotonic() - began >= 0.5

    # Above or below the version, it answers at once.
    began = time.monotonic()
    assert version(api, after=now + 5, timeout=10) == now
    assert version(api, after=now - 1, timeout=10) == now
    assert time.monotonic() - began < 5

    for refused in ({"timeout": 31}, {"timeout": -1}, {"after": -1}):
        assert api.get("/api/changes", params=refused).status_code == 422

    # A turn started while it waits wakes it, long before its timeout.
    woke: list[int] = []
    waiting = threading.Thread(target=lambda: woke.append(version(api, after=now, timeout=20)))
    began = time.monotonic()
    waiting.start()
    time.sleep(0.3)
    last = api.get(f"/api/conversations/{cid}").json()["messages"][-1]["id"]
    turn = api.post(f"/api/conversations/{cid}/turns", json={"parent_id": last, "text": "more"})
    assert turn.status_code == 200, turn.text
    waiting.join(20)
    assert len(woke) == 1
    assert woke[0] > now
    assert time.monotonic() - began < 10

    listed = api.get("/api/conversations").json()["items"]
    assert next(c["running"] for c in listed if c["id"] == cid) is False


# --- two users ----------------------------------------------------------------

PUBLIC_URL = "https://robinauts.example.com"
CONFIG = Config(
    providers={"echo": ProviderConfig("echo", ProviderKind.ANTHROPIC, "ECHO_API_KEY")},
    models={"echo": ModelConfig("echo", provider="echo", name="echo", title="Echo")},
    agents={
        "echo": AgentConfig("echo", title="Echo", system_prompt="", model="echo", engine="echo")
    },
)
SIGN_IN = SignInConfig(
    public_url=PUBLIC_URL,
    providers={
        "okta": SignInProvider(
            id="okta",
            title="Okta",
            issuer="https://example.okta.com/oauth2/default",
            client_id="robinauts",
            client_secret_env="ROBINAUTS_OKTA_SECRET",
        )
    },
    allow=(AllowEntry("okta", Matcher.EVERYONE, None),),
)


@asyncio_test
async def test_another_users_change_neither_wakes_nor_shows_in_the_feed() -> None:
    async with temporary_schema() as schema:
        dsn = f"{url()}{'&' if '?' in url() else '?'}search_path={schema.name}"
        composed = compose(
            CONFIG, storage=StorageConfig(StorageKind.POSTGRES, url=dsn), secret_for={}.get
        )
        async with running(composed, sign_in=SIGN_IN, secret_for={}.get) as app:

            async def bearer(subject: str) -> dict[str, str]:
                person: User = await composed.controller.ensure_user(Identity("okta", subject))
                secret, now = random_secret(), datetime.now(UTC)
                token = ApiToken(
                    uuid.uuid4(), person.id, "t", secret_hash(secret), now, now + timedelta(days=1)
                )
                await composed.credentials.add_api_token(token)
                return {"authorization": f"Bearer {secret}"}

            ada, bob = await bearer("ada"), await bearer("bob")
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url=PUBLIC_URL) as http:

                async def feed(who: dict[str, str], **query: float) -> int:
                    answered = await http.get("/api/changes", params=query, headers=who)
                    assert answered.status_code == 200, answered.text
                    return answered.json()["version"]

                assert await feed(ada) == 0
                waiting = asyncio.create_task(feed(ada, after=0, timeout=1.5))
                await asyncio.sleep(0.2)
                started = await http.post(
                    "/api/turns", json={"agent_id": "echo", "text": "bob's"}, headers=bob
                )
                assert started.status_code == 200, started.text
                began = time.monotonic()
                assert await waiting == 0
                assert time.monotonic() - began >= 1.0
                assert await feed(bob) >= 2
                listed = await http.get("/api/conversations", headers=ada)
                assert listed.json()["items"] == []
