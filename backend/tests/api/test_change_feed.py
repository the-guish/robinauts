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

import httpx
from oneberth.identity import Identity
from oneberth.web import testing

from robinauts.cli.composition import compose
from robinauts.shared_domain.domain import (
    AgentConfig,
    Config,
    ModelConfig,
    ProviderConfig,
    ProviderKind,
    StorageConfig,
    StorageKind,
)
from robinauts.web.app import NAME
from util.aio import asyncio_test
from util.app import backend_of
from util.controller_db import requires_postgres, temporary_schema, url

pytestmark = requires_postgres


def version(api: httpx.Client, **query: float) -> int:
    answered = api.get("/robinauts/api/changes", params=query)
    assert answered.status_code == 200, answered.text
    assert answered.json().keys() == {"version"}
    return answered.json()["version"]


def test_the_feed_answers_at_once_waits_while_unchanged_and_wakes_on_a_change(
    api: httpx.Client,
) -> None:
    started = api.post("/robinauts/api/turns", json={"agent_id": "pydantic_ai", "text": "feed"})
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
        assert api.get("/robinauts/api/changes", params=refused).status_code == 422

    # A turn started while it waits wakes it, long before its timeout.
    woke: list[int] = []
    waiting = threading.Thread(target=lambda: woke.append(version(api, after=now, timeout=20)))
    began = time.monotonic()
    waiting.start()
    time.sleep(0.3)
    last = api.get(f"/robinauts/api/conversations/{cid}").json()["messages"][-1]["id"]
    turn = api.post(
        f"/robinauts/api/conversations/{cid}/turns",
        json={"parent_id": last, "text": "more"},
    )
    assert turn.status_code == 200, turn.text
    waiting.join(20)
    assert len(woke) == 1
    assert woke[0] > now
    assert time.monotonic() - began < 10

    listed = api.get("/robinauts/api/conversations").json()["items"]
    assert next(c["running"] for c in listed if c["id"] == cid) is False


# --- two users ----------------------------------------------------------------

CONFIG = Config(
    providers={"echo": ProviderConfig("echo", ProviderKind.ANTHROPIC, "ECHO_API_KEY")},
    models={"echo": ModelConfig("echo", provider="echo", name="echo", title="Echo")},
    agents={
        "echo": AgentConfig("echo", title="Echo", system_prompt="", models=("echo",), engine="echo")
    },
)


@asyncio_test
async def test_another_users_change_neither_wakes_nor_shows_in_the_feed() -> None:
    async with temporary_schema() as schema:
        dsn = f"{url()}{'&' if '?' in url() else '?'}search_path={schema.name}"
        composed = compose(
            CONFIG,
            storage=StorageConfig(StorageKind.POSTGRES, url=dsn),
            secret_for={}.get,
        )
        backend = backend_of(composed, secret_for={}.get, oneberth=testing.config_for(NAME))
        composition = backend.composition
        async with (
            testing.running(composition) as ada,
            testing.client_of(composition) as bob,
        ):
            await testing.signed_in(composition, ada, Identity("test", "ada"))
            await testing.signed_in(composition, bob, Identity("test", "bob"))

            async def feed(who: httpx.AsyncClient, **query: float) -> int:
                answered = await who.get("/robinauts/api/changes", params=query)
                assert answered.status_code == 200, answered.text
                return answered.json()["version"]

            assert await feed(ada) == 0
            waiting = asyncio.create_task(feed(ada, after=0, timeout=1.5))
            await asyncio.sleep(0.2)
            started = await bob.post(
                "/robinauts/api/turns", json={"agent_id": "echo", "text": "bob's"}
            )
            assert started.status_code == 200, started.text
            began = time.monotonic()
            assert await waiting == 0
            assert time.monotonic() - began >= 1.0
            assert await feed(bob) >= 2
            listed = await ada.get("/robinauts/api/conversations")
            assert listed.json()["items"] == []
