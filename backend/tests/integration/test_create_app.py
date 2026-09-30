# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The whole deployment, wired by ``create_app``, against a real PostgreSQL.

Everything at once and nothing faked but the operator: the configuration file
on disk, the pool on the test database, ``check_schema``, the real HTTP
adapter, and a real OpenID Connect provider on loopback. A person signs in and
out through the routes, and their user and session are rows.

The other half is the refusal: a database with no schema of ours must stop the
server from starting, and say what to run (``docs/specs/backend.md``). The
lifespan is where that happens, so the lifespan is what is driven here.

The local development mode is wired the same way and against the same
database, because the claim it makes is about a real store: its one user is a
row, made once, with an id that a restart finds again.

Each test works in a schema of its own, named in the connection string the way
a deployment would name one.

Marked ``io`` and ``database``; skipped, with the reason, without
``ROBINAUTS_TEST_DATABASE_URL``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest
from ag_ui.core import EventType

from aio import asyncio_test
from conversations import agent_definition, offered
from fakes import ScriptedAgent, says
from postgres import DATABASE_URL, TemporarySchema, requires_postgres
from robinauts.api import CONVERSATION_ID_HEADER, SSE_MEDIA_TYPE
from robinauts.app import create_app
from robinauts.core import message_to_data
from robinauts.datastore import (
    SCHEMA_VERSION,
    PostgresConversationStore,
    create_schema,
)
from robinauts.domain import (
    DB_INIT_COMMAND,
    LOCAL_PROVIDER,
    LOCAL_SUBJECT,
    LOCAL_USER_NAME,
    ConfigError,
    Conversation,
    Message,
    Role,
    SchemaError,
    TextPart,
)
from sse import events
from standin import StandInProvider, redirect_from
from webapp import running

pytestmark = requires_postgres

PUBLIC_URL = "https://robinauts.example.com"
SECRET_VARIABLE = "ROBINAUTS_STAND_IN_SECRET"
KEY_VARIABLE = "ROBINAUTS_ANTHROPIC_KEY"

LOCAL_URL = "http://127.0.0.1:8000"
"""Where the local development mode is served here, port and all."""

LOCAL_WRITE = {"content-type": "application/json", "origin": LOCAL_URL}
"""What a write carries in that mode: JSON, from the origin it was addressed to."""

ASKED = "What is a robinaut?"

ANSWERED = "Someone who plays fair."
"""What the scripted engine of the streaming test answers."""

T0 = datetime(2026, 9, 21, 9, 0, tzinfo=UTC)
"""When the conversation of the test below was written."""

CONFIGURATION = """
public_url = "{public_url}"
session_hours = 1

[providers.standin]
title = "The stand-in"
issuer = "{issuer}"
client_id = "{client_id}"
client_secret_env = "{secret_variable}"

[[allow]]
provider = "standin"
email = "ada@example.com"
"""


@pytest.fixture
def stand_in() -> Iterator[StandInProvider]:
    with StandInProvider() as provider:
        yield provider


def written(tmp_path: Path, stand_in: StandInProvider, extra: str = "") -> Path:
    """The configuration file of a deployment, sign-in half and whatever else.

    ``extra`` is appended as it stands: one file holds the sign-in tables and
    the model tables (``docs/specs/agents.md``), and a test about agents adds
    its own rather than having a second file to keep in step.
    """
    path = tmp_path / "sign-in.toml"
    path.write_text(
        CONFIGURATION.format(
            public_url=PUBLIC_URL,
            issuer=stand_in.issuer,
            client_id=stand_in.client_id,
            secret_variable=SECRET_VARIABLE,
        )
        + extra,
        encoding="utf-8",
    )
    return path


def in_schema(name: str) -> str:
    """The test database's URL, with the search path a deployment would set.

    A deployment that keeps the platform's tables under a schema of its own
    says so in the connection string, and the server obeys it: this is that,
    and it is what lets every test here have a schema to itself.
    """
    assert DATABASE_URL is not None
    separator = "&" if "?" in DATABASE_URL else "?"
    return f"{DATABASE_URL}{separator}options=-csearch_path%3D{name}"


@asynccontextmanager
async def schema(*, applied: bool = True) -> AsyncIterator[TemporarySchema]:
    """A schema of this test's own, with or without ``schema.sql`` in it."""
    temporary = TemporarySchema(min_size=1, max_size=1)
    await temporary.open()
    try:
        if applied:
            await create_schema(temporary.pool)
        yield temporary
    finally:
        await temporary.close()


@asyncio_test
async def test_a_person_signs_in_and_out_of_a_real_deployment(
    tmp_path: Path, stand_in: StandInProvider
) -> None:
    async with schema() as temporary:
        app = create_app(
            config_path=written(tmp_path, stand_in),
            database_url=in_schema(temporary.name),
            secret_for={SECRET_VARIABLE: stand_in.client_secret}.get,
        )

        async with running(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url=PUBLIC_URL,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                begun = await client.get("/auth/login/standin", params={"return_to": "/#/chat/1"})
                back = await redirect_from(begun.headers["location"])
                landed = await client.get(f"/auth/callback/standin?{urlsplit(back).query}")
                signed_in = await client.get("/auth/session")
                out = await client.post(
                    "/auth/logout",
                    headers={"content-type": "application/json", "origin": PUBLIC_URL},
                )
                after = await client.get("/auth/session")
            users = await temporary.pool.fetchval("SELECT count(*) FROM users")
            sessions = await temporary.pool.fetchval("SELECT count(*) FROM sessions")

        closed = app.state.deployment.pool

    assert landed.headers["location"] == f"{PUBLIC_URL}/ui/#/chat/1"
    assert signed_in.json()["user"]["email"] == "ada@example.com"
    assert signed_in.json()["providers"] == [{"id": "standin", "title": "The stand-in"}]
    assert out.status_code == 204
    assert after.json()["user"] is None
    assert (users, sessions) == (1, 0)
    # The lifespan gave back what it took: no pool, no sign-in, nothing held.
    assert closed is None
    assert app.state.sign_in is None
    assert app.state.deployment.sign_in is None


@asyncio_test
async def test_the_local_development_mode_runs_as_one_real_row() -> None:
    """No file, no provider, no session: one user, in the users table, twice over.

    The second ``create_app`` is the restart. It is the whole promise of the
    mode -- the rest of the platform behaves as usual, and what the local user
    owns is still theirs after a restart -- and it is only worth anything
    against a real database, which is why it is here.
    """
    async with schema() as temporary:
        first = create_app(
            local_development_host="127.0.0.1",
            database_url=in_schema(temporary.name),
            secret_for={}.get,
        )
        async with running(first):
            async with local_browser(first) as client:
                opened = await client.get("/auth/session")
                written_by = await temporary.pool.fetchval("SELECT xmin::text FROM users")
                again = await client.get("/auth/session")

        restarted = create_app(
            local_development_host="127.0.0.1",
            database_url=in_schema(temporary.name),
            secret_for={}.get,
        )
        async with running(restarted):
            async with local_browser(restarted) as client:
                after = await client.get("/auth/session")

        rows = await temporary.pool.fetch(
            "SELECT id, provider, subject, name, email, xmin::text AS version FROM users"
        )
        sessions = await temporary.pool.fetchval("SELECT count(*) FROM sessions")

    body = opened.json()
    assert (body["sign_in"], body["local_development"], body["providers"]) == (False, True, [])
    assert body["user"]["id"] == again.json()["user"]["id"] == after.json()["user"]["id"]
    assert len(rows) == 1
    assert str(rows[0]["id"]) == body["user"]["id"]
    assert (rows[0]["provider"], rows[0]["subject"]) == (LOCAL_PROVIDER, LOCAL_SUBJECT)
    assert (rows[0]["name"], rows[0]["email"]) == (LOCAL_USER_NAME, None)
    # Nobody signed in, so nothing was signed in with.
    assert sessions == 0
    # And the four requests after the first one only read: ``xmin`` is the
    # transaction that wrote this row version, so a request that upserted --
    # which is what ``user_at_sign_in`` does -- would have left another one.
    assert rows[0]["version"] == written_by


@asyncio_test
async def test_the_conversation_store_is_opened_on_the_same_pool() -> None:
    # Conversations, messages, runs and events are the same database as users
    # and sessions, so they are the same pool. The deployment holds the store
    # and the three services built on it, it reaches the schema the connection
    # string names, and the lifespan gives all of it back.
    async with schema() as temporary:
        app = create_app(
            local_development_host="127.0.0.1",
            database_url=in_schema(temporary.name),
            secret_for={}.get,
        )

        async with running(app):
            store = app.state.deployment.conversation_store
            assert isinstance(store, PostgresConversationStore)
            assert await store.conversation_by_id(uuid.uuid4()) is None
            assert app.state.deployment.pool is not None
            assert app.state.deployment.conversations is not None
            assert app.state.deployment.turns is not None
            assert app.state.deployment.watch is not None

        assert app.state.deployment.conversation_store is None
        assert app.state.deployment.conversations is None
        assert app.state.deployment.turns is None
        assert app.state.deployment.watch is None
        assert app.state.deployment.pool is None


@asyncio_test
async def test_a_conversation_is_listed_opened_renamed_and_deleted_over_http() -> None:
    """The conversation routes, on the real store, through the real application.

    The routes are proved over the fakes in
    ``tests/unit/test_conversation_routes.py``; what is only worth anything
    here is that the whole of it holds together -- the guard resolving the one
    local user, the services the lifespan opened, the jsonb a message is kept
    as, the keyset the panel is paged with, and a delete that really removes
    the row.

    The conversation is put in the store directly, so that this test is about
    the plain JSON half of the wire alone: starting a turn writes one too, and
    it is the subject of the test below.
    """
    async with schema() as temporary:
        app = create_app(
            local_development_host="127.0.0.1",
            database_url=in_schema(temporary.name),
            secret_for={}.get,
        )

        async with running(app):
            async with local_browser(app) as client:
                whoever = await client.get("/auth/session")
                owner = uuid.UUID(whoever.json()["user"]["id"])
                kept = Conversation(
                    id=uuid.uuid4(),
                    owner_id=owner,
                    agent="assistant",
                    model="sonnet",
                    created_at=T0,
                    updated_at=T0,
                    title=ASKED,
                )
                asked = Message(
                    id=uuid.uuid4(),
                    conversation_id=kept.id,
                    parent_id=None,
                    role=Role.USER,
                    parts=(TextPart(ASKED),),
                    created_at=T0,
                )
                store = app.state.deployment.conversation_store
                await store.add_conversation(kept)
                await store.append_message(asked, message_to_data(asked), now=T0)

                listed = await client.get("/api/conversations", params={"limit": 1})
                opened = await client.get(f"/api/conversations/{kept.id}")
                agents = await client.get("/api/agents")
                renamed = await client.patch(
                    f"/api/conversations/{kept.id}",
                    json={"title": "Robinauts"},
                    headers=LOCAL_WRITE,
                )
                deleted = await client.delete(f"/api/conversations/{kept.id}", headers=LOCAL_WRITE)
                gone = await client.get(f"/api/conversations/{kept.id}")

            rows = await temporary.pool.fetchval("SELECT count(*) FROM conversations")
            messages = await temporary.pool.fetchval("SELECT count(*) FROM messages")

    assert [item["id"] for item in listed.json()["items"]] == [str(kept.id)]
    assert listed.json()["next_cursor"] is None
    assert opened.json()["messages"] == [
        {
            "id": str(asked.id),
            "role": "user",
            "channel": "web",
            "created_at": "2026-09-21T09:00:00Z",
            "parts": [{"kind": "text", "text": ASKED}],
            "provenance": None,
        }
    ]
    # The local development mode has no configuration file at all, so it has
    # no agents: the picker is told so rather than left to guess
    # (docs/working-notes/poc-scope.md).
    assert agents.json() == {"items": []}
    assert renamed.json()["title"] == "Robinauts"
    assert deleted.status_code == 204
    assert gone.status_code == 404
    # The delete took the conversation and the message under it.
    assert (rows, messages) == (0, 0)


@asyncio_test
async def test_a_turn_is_streamed_and_what_it_produced_is_in_the_conversation() -> None:
    """The streaming half of the wire, end to end on the real store.

    The mapping and the routes are proved over the fakes
    (``tests/unit/test_agui.py``, ``test_stream_routes.py``); what only this
    can show is the whole of it together: an agent configured at start-up, a
    run executed in the background by the deployment's own executor, every
    event of it written to PostgreSQL as jsonb and read back, and the answer
    in the conversation afterwards -- which is what a person who closed the tab
    would come back to.

    The engine is the scripted one: no provider is reached, and what a real
    engine puts in its place is the agent port (``docs/layout.md``).
    """
    definition = agent_definition()
    async with schema() as temporary:
        app = create_app(
            local_development_host="127.0.0.1",
            database_url=in_schema(temporary.name),
            secret_for={}.get,
            agents={definition.id: definition},
            models=offered(),
            engines={definition.engine: ScriptedAgent(*says(ANSWERED))},
        )

        async with running(app):
            async with local_browser(app) as client:
                streamed = await client.post(
                    "/api/turns",
                    json={"agent_id": definition.id, "text": ASKED},
                    headers=LOCAL_WRITE,
                )
                conversation_id = streamed.headers[CONVERSATION_ID_HEADER]
                opened = await client.get(f"/api/conversations/{conversation_id}")

            positions = await temporary.pool.fetchval("SELECT count(*) FROM run_events")

    assert streamed.status_code == 200
    assert streamed.headers["content-type"].startswith(SSE_MEDIA_TYPE)
    sent = events(streamed.text)
    assert [block.type for block in sent] == [
        EventType.RUN_STARTED.value,
        EventType.TEXT_MESSAGE_START.value,
        EventType.TEXT_MESSAGE_CONTENT.value,
        EventType.TEXT_MESSAGE_END.value,
        EventType.RUN_FINISHED.value,
    ]
    # Every event the platform stored was sent, under its own position.
    assert [block.id for block in sent] == [str(seq) for seq in range(1, positions + 1)]
    # And the answer is in the conversation, not only in the stream.
    said = [[part["text"] for part in message["parts"]] for message in opened.json()["messages"]]
    assert said == [[ASKED], [ANSWERED]]
    assert opened.json()["run_id"] is None


def local_browser(app: object) -> httpx.AsyncClient:
    """A browser on the loopback address the mode is served on, port and all."""
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=LOCAL_URL,
        follow_redirects=False,
        trust_env=False,
    )


@asyncio_test
async def test_the_server_refuses_to_start_against_a_database_with_no_schema(
    tmp_path: Path, stand_in: StandInProvider
) -> None:
    """And says what to run, and that it works on an empty database only."""
    async with schema(applied=False) as temporary:
        app = create_app(
            config_path=written(tmp_path, stand_in),
            database_url=in_schema(temporary.name),
            secret_for={SECRET_VARIABLE: stand_in.client_secret}.get,
        )

        with pytest.raises(SchemaError) as raised:
            async with running(app):  # pragma: no cover -- start-up fails
                pass

        # The pool it opened to find out is closed all the same.
        assert app.state.deployment.pool is None

    assert "no Robinauts schema" in str(raised.value)
    assert DB_INIT_COMMAND in str(raised.value)
    assert f"schema version {SCHEMA_VERSION}" in str(raised.value)
    assert app.state.sign_in is None


@asyncio_test
async def test_the_pool_is_opened_only_when_the_lifespan_runs(
    tmp_path: Path, stand_in: StandInProvider
) -> None:
    """``create_app`` reads and validates; it opens nothing.

    So a misconfigured deployment fails before it touches a database, and a
    process that is only being asked for its application does not connect.
    """
    async with schema() as temporary:
        app = create_app(
            config_path=written(tmp_path, stand_in),
            database_url=in_schema(temporary.name),
            secret_for={SECRET_VARIABLE: stand_in.client_secret}.get,
        )

        assert app.state.deployment.pool is None

        async with running(app):
            opened = app.state.deployment.pool
            assert not opened.is_closing()

    assert opened.is_closing()


AGENT_TABLES = f"""
[model_providers.anthropic]
kind = "anthropic"
api_key_env = "{KEY_VARIABLE}"

[models.sonnet]
provider = "anthropic"
name = "claude-sonnet-5"

[agents.assistant]
title = "Assistant"
model = "sonnet"
engine = "langgraph"
system_prompt = "Play fair."
"""
"""The model half of the same file: one provider, one model, one agent."""


@asyncio_test
async def test_an_agent_in_the_configuration_is_offered_to_whoever_signed_in(
    tmp_path: Path, stand_in: StandInProvider
) -> None:
    """The model half of the file, end to end: read, keyed, wired, served.

    Nothing reaches Anthropic -- the key is read and never spent, and no turn
    is started -- so what this proves is the wiring: the tables parsed at
    start-up, the key found in the environment, the LangGraph engine built,
    and the agent on the list the picker is drawn from.
    """
    async with schema() as temporary:
        app = create_app(
            config_path=written(tmp_path, stand_in, AGENT_TABLES),
            database_url=in_schema(temporary.name),
            secret_for={
                SECRET_VARIABLE: stand_in.client_secret,
                KEY_VARIABLE: "sk-not-a-real-key",
            }.get,
        )

        async with running(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url=PUBLIC_URL,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                anonymous = await client.get("/api/agents")
                begun = await client.get("/auth/login/standin")
                back = await redirect_from(begun.headers["location"])
                await client.get(f"/auth/callback/standin?{urlsplit(back).query}")
                agents = await client.get("/api/agents")
                models = await client.get("/api/models")

    # A picker is for people who signed in, like everything else here.
    assert anonymous.status_code == 401
    assert agents.json() == {
        "items": [
            {"id": "assistant", "title": "Assistant", "engine": "langgraph", "model": "sonnet"}
        ]
    }
    # No title in the file: the model is called by its id, and the vendor's
    # name for it stays the operator's.
    assert models.json() == {"items": [{"id": "sonnet", "title": "sonnet"}]}
    assert "claude-sonnet-5" not in models.text


def test_an_unset_model_key_stops_the_server_before_it_binds_a_port(
    tmp_path: Path, stand_in: StandInProvider
) -> None:
    # Named at start-up, by the variable and never by a value
    # (docs/working-notes/poc-scope.md, "Done when" 5).
    with pytest.raises(ConfigError) as raised:
        create_app(
            config_path=written(tmp_path, stand_in, AGENT_TABLES),
            database_url="postgresql://nobody@127.0.0.1:1/never-opened",
            secret_for={SECRET_VARIABLE: stand_in.client_secret}.get,
        )

    assert list(raised.value.problems) == [
        f"model_providers.anthropic: the API key is read from the environment variable"
        f" {KEY_VARIABLE}, which is unset or empty"
    ]


def models_only(tmp_path: Path) -> Path:
    """A configuration file with the model tables and no sign-in in it.

    What the local development mode may be given: the chat is developed in
    that mode (``docs/specs/frontend.md``) and a chat needs an agent, but the
    mode exists where there is nothing to sign in to, so the sign-in half must
    not be there.
    """
    path = tmp_path / "models.toml"
    path.write_text(AGENT_TABLES, encoding="utf-8")
    return path


@asyncio_test
async def test_the_local_development_mode_serves_the_agents_of_its_own_file(
    tmp_path: Path,
) -> None:
    """No sign-in, one local user, and a real agent to develop the chat against.

    Whole, on the real PostgreSQL: the model tables read at start-up, the key
    found in the environment, the LangGraph engine built, and the agent listed
    to a browser that never signed in to anything.
    """
    async with schema() as temporary:
        app = create_app(
            local_development_host="127.0.0.1",
            config_path=models_only(tmp_path),
            database_url=in_schema(temporary.name),
            secret_for={KEY_VARIABLE: "sk-not-a-real-key"}.get,
        )

        async with running(app):
            assert app.state.sign_in is None
            async with local_browser(app) as client:
                whoever = await client.get("/auth/session")
                agents = await client.get("/api/agents")

    assert whoever.json()["local_development"] is True
    assert agents.json() == {
        "items": [
            {"id": "assistant", "title": "Assistant", "engine": "langgraph", "model": "sonnet"}
        ]
    }


def test_the_local_development_mode_refuses_a_file_that_signs_anybody_in(
    tmp_path: Path, stand_in: StandInProvider
) -> None:
    with pytest.raises(ConfigError) as raised:
        create_app(
            local_development_host="127.0.0.1",
            config_path=written(tmp_path, stand_in, AGENT_TABLES),
            database_url="postgresql://nobody@127.0.0.1:1/never-opened",
            secret_for={KEY_VARIABLE: "sk-not-a-real-key"}.get,
        )

    assert any("cannot be combined" in problem for problem in raised.value.problems)
