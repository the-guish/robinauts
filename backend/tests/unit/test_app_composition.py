# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The composition root: what it refuses at start-up, and what it opens and closes.

Two halves, and they are tested apart because they fail apart. Configuring is
where an operator's mistakes are found, and the promise is that **every one of
them is reported at once** -- a start-up that named one problem per restart
would cost as many restarts as there are mistakes. Opening is where a process
takes hold of something, and the promise is that the lifespan gives all of it
back.

What a process holds now includes **work**: the runs it is answering. So the
last part of this module is about the two moments that belong to a process
rather than to a request -- the start-up sweep, which ends the runs a process
that went away left going, and the shutdown, which stops the runs this one was
answering and records them as ``interrupted`` rather than as cancelled.

No database and no environment variable here: the collaborators are handed in,
which is what ``Deployment`` takes them for.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from pathlib import Path

import httpx
import pytest

from aio import asyncio_test
from conversations import (
    AGENT,
    CONVERSATION,
    agent_definition,
    at,
    conversation,
    model_config,
    offered,
    question,
    run,
)
from fakes import (
    FakeClock,
    Gate,
    MemoryConversationStore,
    MemoryCredentialStore,
    ScriptedAgent,
    ScriptedIdentityProvider,
    says,
)
from robinauts import app as app_module
from robinauts.adapters import HttpIdentityProvider, SecretLookup
from robinauts.app import (
    AUTH_CONFIG_VARIABLE,
    CONFIG_VARIABLE,
    DATABASE_URL_VARIABLE,
    SHUTDOWN_SECONDS,
    WIRED_ENGINES,
    Deployment,
    create_app,
)
from robinauts.application import DEFAULT_TURN_SECONDS, ENDING_BUDGET_SECONDS, TIMED_OUT
from robinauts.core import message_to_data
from robinauts.domain import (
    ACTIVE_RUN_STATES,
    ConfigError,
    Engine,
    InvalidValueError,
    Run,
    RunQuietError,
    RunState,
    TextDelta,
)
from turns import AUTHOR, readable, stored_events
from webapp import PUBLIC_URL, running

pytestmark = pytest.mark.io  # every test here writes a configuration file

DATABASE_URL = "postgresql://nobody@127.0.0.1:1/never-opened"

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


def written(tmp_path: Path, text: str = CONFIGURATION) -> Path:
    path = tmp_path / "sign-in.toml"
    path.write_text(text, encoding="utf-8")
    return path


def reading(variables: Mapping[str, str]) -> SecretLookup:
    """A ``SecretLookup`` over a mapping: the environment, without one.

    No test here sets a real variable: a secret in the process's environment
    is a secret in every other test's environment too.
    """
    return variables.get


def deployed(tmp_path: Path, **changes: object) -> Deployment:
    """A configured deployment over fakes, its file written for it.

    Anything named in ``changes`` replaces what is here, so a test that is
    about a store, an engine or an agent hands in its own.
    """
    fields: dict[str, object] = {
        "config_path": written(tmp_path),
        "database_url": DATABASE_URL,
        "secret_for": reading({"ROBINAUTS_GOOGLE_SECRET": "a-secret"}),
        "credentials": MemoryCredentialStore(),
        "conversation_store": MemoryConversationStore(),
        "clock": FakeClock(),
    }
    fields.update(changes)
    return Deployment.configured(**fields)  # type: ignore[arg-type]


# Configuring.


def test_a_file_that_does_not_parse_is_the_whole_story(tmp_path: Path) -> None:
    """There is no configuration to check secrets against, so none are checked."""
    with pytest.raises(ConfigError) as raised:
        Deployment.configured(
            config_path=written(tmp_path, "public_url = "),
            database_url=DATABASE_URL,
            secret_for=reading({}),
            credentials=MemoryCredentialStore(),
            conversation_store=MemoryConversationStore(),
        )

    assert len(raised.value.problems) == 1
    assert "not valid TOML" in raised.value.problems[0]


def test_every_problem_of_a_usable_configuration_is_reported_at_once(
    tmp_path: Path,
) -> None:
    """The client secret and the database, in one refusal, not two restarts.

    That is what ``docs/specs/operations.md`` asks of start-up, and the
    composition root is the only place that has both: ``core`` cannot see the
    environment and ``check_client_secrets`` cannot see the database.
    """
    with pytest.raises(ConfigError) as raised:
        Deployment.configured(
            config_path=written(tmp_path), database_url=None, secret_for=reading({})
        )

    problems = "\n".join(raised.value.problems)
    assert len(raised.value.problems) == 2
    assert "ROBINAUTS_GOOGLE_SECRET" in problems
    assert DATABASE_URL_VARIABLE in problems


def test_a_configuration_core_refuses_is_reported_with_what_else_is_wrong(
    tmp_path: Path,
) -> None:
    """Core's problems and the root's, together; the secrets are not among them.

    There is no configuration for them to be checked against -- a provider
    whose table was thrown out has no variable to look for -- so the honest
    answer is the problems that could be found, not a guess at the rest.
    """
    broken = CONFIGURATION.replace("[[allow]]", "[[nothing]]")

    with pytest.raises(ConfigError) as raised:
        Deployment.configured(
            config_path=written(tmp_path, broken),
            database_url=None,
            secret_for=reading({}),
        )

    problems = "\n".join(raised.value.problems)
    assert "nothing" in problems
    assert "no entry, so nobody could sign in" in problems
    assert DATABASE_URL_VARIABLE in problems
    assert "ROBINAUTS_GOOGLE_SECRET" not in problems


def test_the_missing_client_secret_is_named_and_never_its_value(
    tmp_path: Path,
) -> None:
    with pytest.raises(ConfigError) as raised:
        Deployment.configured(
            config_path=written(tmp_path),
            database_url=DATABASE_URL,
            secret_for=reading({}),
            credentials=MemoryCredentialStore(),
            conversation_store=MemoryConversationStore(),
        )

    assert raised.value.problems == (
        "providers.google: the client secret is read from the environment variable"
        " ROBINAUTS_GOOGLE_SECRET, which is unset or empty",
    )


def test_the_file_and_the_database_are_read_from_the_environment(
    tmp_path: Path,
) -> None:
    path = written(tmp_path)

    deployment = Deployment.configured(
        secret_for=reading(
            {
                CONFIG_VARIABLE: str(path),
                DATABASE_URL_VARIABLE: DATABASE_URL,
                "ROBINAUTS_GOOGLE_SECRET": "a-secret",
            }
        ),
    )

    assert deployment.config.public_url == "https://robinauts.example.com"
    assert deployment.database_url == DATABASE_URL


def test_a_deployment_that_was_told_nothing_says_what_to_set() -> None:
    with pytest.raises(ConfigError) as raised:
        Deployment.configured(secret_for=reading({}))

    problems = "\n".join(raised.value.problems)
    assert CONFIG_VARIABLE in problems
    assert DATABASE_URL_VARIABLE in problems


def test_the_stores_that_were_handed_in_need_no_database_url(tmp_path: Path) -> None:
    """What the process does not open, it does not need to be told about."""
    deployment = Deployment.configured(
        config_path=written(tmp_path),
        secret_for=reading({"ROBINAUTS_GOOGLE_SECRET": "a-secret"}),
        credentials=MemoryCredentialStore(),
        conversation_store=MemoryConversationStore(),
    )

    assert deployment.database_url is None


@pytest.mark.parametrize("handed_in", ["credentials", "conversation_store"])
def test_one_store_handed_in_and_no_database_is_refused(tmp_path: Path, handed_in: str) -> None:
    # Both stores are the same database, and a deployment that is open has
    # both. Handing in one of them and naming no database would otherwise
    # open a process with the other one missing: it would start, serve, and
    # fail on the first request that needed it.
    stores = {
        "credentials": MemoryCredentialStore(),
        "conversation_store": MemoryConversationStore(),
    }

    with pytest.raises(ConfigError) as raised:
        Deployment.configured(
            config_path=written(tmp_path),
            secret_for=reading({"ROBINAUTS_GOOGLE_SECRET": "a-secret"}),
            **{handed_in: stores[handed_in]},
        )

    assert DATABASE_URL_VARIABLE in "\n".join(raised.value.problems)


# Opening and closing.


@asyncio_test
async def test_opening_wires_the_application_and_closing_lets_it_go(
    tmp_path: Path,
) -> None:
    deployment = deployed(tmp_path, provider=ScriptedIdentityProvider())

    sign_in = await deployment.open()
    opened = deployment.sign_in

    await deployment.aclose()

    assert opened is sign_in
    assert sign_in.config.public_url == "https://robinauts.example.com"
    assert deployment.sign_in is None


@asyncio_test
async def test_an_identity_provider_that_holds_a_client_is_closed(
    tmp_path: Path,
) -> None:
    """The process holds it for its life, and shutting down is when it goes."""
    adapter = HttpIdentityProvider(secret_for=reading({}), trust_env=False)
    deployment = deployed(tmp_path, provider=adapter)

    await deployment.open()
    await deployment.aclose()

    # The adapter makes its own client and `aclose` is the only way to close
    # it; a client nobody closed is a warning at shutdown and a leaked socket.
    with pytest.raises(RuntimeError):
        await adapter.discovery_document(deployment.config.provider("google"))


@asyncio_test
async def test_a_deployment_is_opened_once(tmp_path: Path) -> None:
    """A second open would leave the first pool and client unreachable and held.

    Connections for the life of the process, against a deployment that thinks
    it has one set of them. It is a mistake to say so about, not to carry out.
    """
    deployment = deployed(tmp_path, provider=ScriptedIdentityProvider())
    first = await deployment.open()

    with pytest.raises(InvalidValueError) as raised:
        await deployment.open()

    assert "opened already" in str(raised.value)
    assert deployment.sign_in is first
    await deployment.aclose()


@asyncio_test
async def test_it_is_still_opened_once_after_it_has_been_closed(tmp_path: Path) -> None:
    deployment = deployed(tmp_path, provider=ScriptedIdentityProvider())
    await deployment.open()
    await deployment.aclose()

    with pytest.raises(InvalidValueError):
        await deployment.open()


@asyncio_test
async def test_closing_one_that_was_never_opened_does_nothing(tmp_path: Path) -> None:
    deployment = deployed(tmp_path, provider=ScriptedIdentityProvider())

    await deployment.aclose()

    assert (deployment.sign_in, deployment.pool) == (None, None)


@asyncio_test
async def test_closing_twice_is_the_same_as_closing_once(tmp_path: Path) -> None:
    """Each closer is taken off the list before it is called, so there is
    nothing left for a second call to close twice -- which a lifespan that
    unwinds twice, and a test with a ``finally``, both rely on."""
    adapter = HttpIdentityProvider(secret_for=reading({}), trust_env=False)
    deployment = deployed(tmp_path, provider=adapter)

    await deployment.open()
    await deployment.aclose()
    await deployment.aclose()

    assert (deployment.sign_in, deployment.pool) == (None, None)


@asyncio_test
async def test_the_lifespan_opens_before_the_first_request_and_closes_after(
    tmp_path: Path,
) -> None:
    """The application answers nobody until the lifespan has run, and lets go after."""
    app = create_app(
        config_path=written(tmp_path),
        secret_for=reading({"ROBINAUTS_GOOGLE_SECRET": "a-secret"}),
        credentials=MemoryCredentialStore(),
        conversation_store=MemoryConversationStore(),
        provider=ScriptedIdentityProvider(),
        clock=FakeClock(),
    )

    assert app.state.sign_in is None
    async with running(app):
        assert app.state.sign_in is not None
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=PUBLIC_URL
        ) as client:
            answered = await client.get("/auth/session")

    assert answered.json() == {
        "sign_in": True,
        "local_development": False,
        "public_url": PUBLIC_URL,
        "providers": [{"id": "google", "title": "Google"}],
        "user": None,
    }
    assert app.state.sign_in is None
    assert app.state.deployment.sign_in is None


TURN_SECONDS = 0.05
"""A turn timeout short enough that both of its sides can be waited out."""


@asyncio_test
async def test_one_turn_timeout_bounds_a_turn_and_the_watching_of_one(tmp_path: Path) -> None:
    # One number, two sides of the same question: how long a turn may take,
    # and how long a watcher follows a run that has stored nothing before it
    # gives up on it. A deployment that shortened the first and left the
    # second would hold a request open long after it had failed the run the
    # request was watching -- so both are this, and both are waited out here.
    held = Gate()
    store = MemoryConversationStore()
    deployment = answering(tmp_path, held, store=store, turn_seconds=TURN_SECONDS)
    assert deployment.turn_seconds == TURN_SECONDS
    await deployment.open()
    assert deployment.turns is not None and deployment.watch is not None

    # Watching: a run nothing here is answering, written after the start-up
    # sweep, stores nothing and is given up on -- which is said and not
    # guessed (`domain.RunQuietError`).
    going = await left_running(store)
    with pytest.raises(RunQuietError):
        await anext(deployment.watch.events(AUTHOR, going.id))

    # The turn: an engine that never answers is stopped by the same number,
    # and the run is failed for it rather than left running.
    started = await deployment.turns.begin(AUTHOR, agent_id=AGENT, text="What is a robinaut?")
    while (await store.run_by_id(started.run.id)).state in ACTIVE_RUN_STATES:
        await asyncio.sleep(0.005)
    failed = await store.run_by_id(started.run.id)
    assert failed.state is RunState.FAILED and failed.error == TIMED_OUT
    await deployment.aclose()

    # And a deployment that asks for no particular one has the application's,
    # which is the same number for both of them too.
    assert deployed(tmp_path).turn_seconds == DEFAULT_TURN_SECONDS


def test_the_shutdown_bound_is_added_up_from_the_endings_it_waits_for() -> None:
    # Not a number chosen here: a cancelled run writes its ending under a
    # shield, a few bounded attempts of it, and the application is what says
    # how long all of that may take. A shutdown bound shorter than that would
    # abandon writes that were about to land and leave runs `running` for the
    # next start-up sweep to find.
    assert SHUTDOWN_SECONDS > ENDING_BUDGET_SECONDS


@asyncio_test
async def test_create_app_refuses_a_configuration_it_cannot_use(tmp_path: Path) -> None:
    """``robinauts start`` fails before it binds a port, not after."""
    with pytest.raises(ConfigError):
        create_app(
            config_path=written(tmp_path),
            secret_for=reading({}),
            credentials=MemoryCredentialStore(),
            conversation_store=MemoryConversationStore(),
        )


# Background work: the sweep at start-up, and the runs a shutdown stops.


async def left_running(store: MemoryConversationStore) -> Run:
    """A conversation whose run is still going, as a process that died left it.

    Written straight into the store, because that is what it is: rows from
    before this process existed, with nothing in memory that knows about them.
    """
    asked = question(conversation_id=CONVERSATION)
    going = run(conversation_id=CONVERSATION, message_id=asked.id)
    await store.start_run(
        conversation=conversation(),
        message=(asked, message_to_data(asked)),
        run=going,
        now=at(0),
    )
    return going


def answering(
    tmp_path: Path, *steps: object, store: MemoryConversationStore, **changes: object
) -> Deployment:
    """A deployment whose one agent runs that script over that store."""
    definition = agent_definition()
    return deployed(
        tmp_path,
        conversation_store=store,
        agents={definition.id: definition},
        models=offered(),
        engines={definition.engine: ScriptedAgent(*steps)},  # type: ignore[arg-type]
        provider=ScriptedIdentityProvider(),
        **changes,
    )


@asyncio_test
async def test_the_start_up_sweep_ends_the_runs_a_process_that_went_away_left(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # The POC is one process, so a run still going when this one starts is a
    # run whose process is gone. It is ended with the event that says so --
    # otherwise its conversation refuses every new message and a watcher of it
    # waits for an answer nobody is writing.
    store = MemoryConversationStore()
    going = await left_running(store)
    deployment = deployed(tmp_path, conversation_store=store, provider=ScriptedIdentityProvider())

    with caplog.at_level(logging.INFO):
        await deployment.open()
    await deployment.aclose()

    ended = await store.run_by_id(going.id)
    assert ended is not None and ended.state is RunState.INTERRUPTED
    readable(await stored_events(store, going.id), ended)
    assert "1 run" in caplog.text


@asyncio_test
async def test_a_sweep_that_fails_does_not_stop_the_deployment(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # Housekeeping, not correctness: the schema was checked a moment ago, so a
    # sweep that cannot read is a store that went away between two calls, and
    # a server that refused to start over it would be down for a reason
    # nobody asked about.
    class Unreadable(MemoryConversationStore):
        async def runs_in(self, states: object, *, limit: int = 1) -> tuple[Run, ...]:
            raise OSError("the database went away")

    deployment = deployed(
        tmp_path, conversation_store=Unreadable(), provider=ScriptedIdentityProvider()
    )

    with caplog.at_level(logging.ERROR):
        assert await deployment.open() is not None
    await deployment.aclose()

    assert "sweep" in caplog.text


@asyncio_test
async def test_opening_wires_the_services_the_routes_call(tmp_path: Path) -> None:
    deployment = deployed(tmp_path, provider=ScriptedIdentityProvider())

    await deployment.open()

    assert deployment.conversations is not None
    assert deployment.turns is not None
    assert deployment.watch is not None
    assert deployment.conversation_store is not None

    await deployment.aclose()

    assert deployment.conversations is None
    assert deployment.turns is None
    assert deployment.watch is None


@asyncio_test
async def test_shutting_down_interrupts_the_runs_this_process_was_answering(
    tmp_path: Path,
) -> None:
    # Nobody cancelled these runs: the process went away with them. The
    # difference is the author's -- an interrupted run is theirs to retry --
    # and it is written into the record and announced to whoever is watching.
    held = Gate()
    store = MemoryConversationStore()
    deployment = answering(tmp_path, TextDelta(text="Half of an"), held, store=store)
    await deployment.open()
    assert deployment.turns is not None
    started = await deployment.turns.begin(AUTHOR, agent_id=AGENT, text="What is a robinaut?")
    await held.reached.wait()
    while await store.last_position(started.run.id) < 3:
        await asyncio.sleep(0)

    await deployment.aclose()

    ended = await store.run_by_id(started.run.id)
    assert ended is not None and ended.state is RunState.INTERRUPTED
    events = await stored_events(store, started.run.id)
    readable(events, ended)
    assert events[-1].event.state is RunState.INTERRUPTED


@asyncio_test
async def test_a_turn_whose_work_never_began_is_ended_while_the_stores_are_open(
    tmp_path: Path,
) -> None:
    # The window a shutdown can land in: the work was handed over and the
    # process began stopping before the loop stepped it. Nothing of the turn
    # ran, so the executor says so -- and it says so **before** the stores are
    # closed, which is the whole reason it is closed before them.
    store = MemoryConversationStore()
    deployment = answering(tmp_path, *says("Someone who plays fair."), store=store)
    await deployment.open()
    assert deployment.turns is not None
    started = await deployment.turns.begin(AUTHOR, agent_id=AGENT, text="What is a robinaut?")

    await deployment.aclose()

    ended = await store.run_by_id(started.run.id)
    assert ended is not None and ended.state is RunState.INTERRUPTED
    readable(await stored_events(store, started.run.id), ended)


@asyncio_test
async def test_a_turn_begun_through_the_deployment_runs_to_its_end(tmp_path: Path) -> None:
    store = MemoryConversationStore()
    deployment = answering(tmp_path, *says("Someone who plays fair."), store=store)
    await deployment.open()
    assert deployment.turns is not None and deployment.watch is not None

    started = await deployment.turns.begin(AUTHOR, agent_id=AGENT, text="What is a robinaut?")
    seen = [event async for event in deployment.watch.events(AUTHOR, started.run.id)]

    readable(seen, started.run)
    ended = await store.run_by_id(started.run.id)
    assert ended is not None and ended.state is RunState.FINISHED
    await deployment.aclose()


# The model half of the configuration: the agents, their keys and their engine.


MODEL_TABLES = """
[model_providers.anthropic]
kind = "anthropic"
api_key_env = "ROBINAUTS_ANTHROPIC_KEY"

[models.sonnet]
provider = "anthropic"
name = "claude-sonnet-5"

[agents.assistant]
title = "Assistant"
model = "sonnet"
engine = "langgraph"
system_prompt = "Play fair."
"""
"""The model half, appended to the sign-in half: one file holds both."""

WITH_AGENTS = CONFIGURATION + MODEL_TABLES

BOTH_KEYS = {"ROBINAUTS_GOOGLE_SECRET": "a-secret", "ROBINAUTS_ANTHROPIC_KEY": "sk-not-a-real-key"}


def with_agents(tmp_path: Path, text: str = WITH_AGENTS, **changes: object) -> Deployment:
    """A deployment configured from a file that holds both halves.

    In a directory of its own, because ``deployed`` writes its own file at
    ``tmp_path`` under the same name and would overwrite this one.
    """
    here = tmp_path / "with-agents"
    here.mkdir(exist_ok=True)
    fields: dict[str, object] = {
        "config_path": written(here, text),
        "secret_for": reading(BOTH_KEYS),
    }
    fields.update(changes)
    return deployed(tmp_path, **fields)


@asyncio_test
async def test_the_agents_of_the_configuration_are_wired_on_the_engine_they_name(
    tmp_path: Path,
) -> None:
    # The whole of the model half, end to end through the composition root:
    # the tables read, the key found, the engine built, and the agent handed
    # to the run lifecycle -- which is what refuses an agent whose engine is
    # not there.
    deployment = with_agents(tmp_path)

    await deployment.open()
    try:
        assert deployment.turns is not None
        assert [definition.id for definition in deployment.turns.agents] == ["assistant"]
        assert deployment.turns.agents[0].engine is Engine.LANGGRAPH
        assert deployment.turns.agents[0].system_prompt == "Play fair."
    finally:
        await deployment.aclose()


@asyncio_test
async def test_a_deployment_whose_file_names_no_agent_starts_with_none(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # Allowed, and said out loud: the picker has nothing in it and
    # /api/agents is empty, which is what the local development mode has too.
    with caplog.at_level(logging.INFO):
        deployment = deployed(tmp_path)

    await deployment.open()
    try:
        assert deployment.turns is not None
        assert deployment.turns.agents == ()
    finally:
        await deployment.aclose()
    assert any("no [agents] table" in record.message for record in caplog.records)


def test_a_model_provider_whose_key_is_unset_stops_the_start_up(tmp_path: Path) -> None:
    # Named at start-up, by the variable, so that a deployment never gets as
    # far as a person waiting for an answer it cannot pay for.
    with pytest.raises(ConfigError) as raised:
        with_agents(tmp_path, secret_for=reading({"ROBINAUTS_GOOGLE_SECRET": "a-secret"}))

    assert list(raised.value.problems) == [
        "model_providers.anthropic: the API key is read from the environment variable"
        " ROBINAUTS_ANTHROPIC_KEY, which is unset or empty"
    ]


WITH_TOOLS = (
    WITH_AGENTS.replace('engine = "langgraph"', 'engine = "langgraph"\ntools = ["github"]') + """
[mcp_servers.github]
url = "https://api.githubcopilot.com/mcp/"
secret_env = "ROBINAUTS_GITHUB_TOKEN"
"""
)
"""The agent of ``WITH_AGENTS`` given a tool server, whose secret is a third variable."""
assert 'tools = ["github"]' in WITH_TOOLS  # the edit above found its line


def test_a_tool_server_whose_secret_is_unset_stops_the_start_up_with_the_rest(
    tmp_path: Path,
) -> None:
    # Named by the variable, together with every other start-up problem: a
    # deployment with two unset variables is fixed in one pass, and start-up
    # connects to no server to find out.
    with pytest.raises(ConfigError) as raised:
        with_agents(tmp_path, WITH_TOOLS, secret_for=reading({"ROBINAUTS_GOOGLE_SECRET": "s"}))

    assert list(raised.value.problems) == [
        "model_providers.anthropic: the API key is read from the environment variable"
        " ROBINAUTS_ANTHROPIC_KEY, which is unset or empty",
        "mcp_servers.github: the secret is read from the environment variable"
        " ROBINAUTS_GITHUB_TOKEN, which is unset or empty",
    ]


def test_a_deployment_that_started_holds_the_tool_secrets_it_read_and_prints_none(
    tmp_path: Path,
) -> None:
    deployment = with_agents(
        tmp_path, WITH_TOOLS, secret_for=reading({**BOTH_KEYS, "ROBINAUTS_GITHUB_TOKEN": "ghp-x"})
    )

    assert deployment.tool_secrets.secret_for("github") == "ghp-x"
    assert repr(deployment.tool_secrets) == "ToolServerSecrets(github)"

    # A public server names no variable, so start-up reads none for it.
    public = (
        WITH_AGENTS.replace('engine = "langgraph"', 'engine = "langgraph"\ntools = ["learn"]') + """
[mcp_servers.learn]
url = "https://learn.microsoft.com/api/mcp"
auth = "none"
"""
    )
    deployment = with_agents(tmp_path, public, secret_for=reading(BOTH_KEYS))
    assert repr(deployment.tool_secrets) == "ToolServerSecrets()"


def test_an_agent_handed_in_naming_a_tool_server_the_configuration_has_not_got_is_refused(
    tmp_path: Path,
) -> None:
    """Refused at configuration, not found out in the middle of somebody's
    turn: the engines built from the file are configured with the file's
    servers and no others, and an agent handed in to run on them names one
    of those -- as the parser holds the file's agents to the file's servers."""
    definition = agent_definition(tools=("github",))

    with pytest.raises(ConfigError) as raised:
        with_agents(tmp_path, agents={definition.id: definition})

    assert list(raised.value.problems) == [
        "the agent 'assistant' that was handed in uses tool server 'github', which is not"
        " in the configuration: configure the server, or hand in the engine that is to"
        " run the agent"
    ]


def test_the_key_itself_is_in_none_of_what_a_start_up_refusal_says(tmp_path: Path) -> None:
    text = WITH_AGENTS.replace(
        'api_key_env = "ROBINAUTS_ANTHROPIC_KEY"', 'api_key_env = "sk-pasted-by-mistake"'
    )

    with pytest.raises(ConfigError) as raised:
        with_agents(tmp_path, text)

    assert "sk-pasted-by-mistake" not in str(raised.value)


@asyncio_test
async def test_an_agent_may_name_either_of_the_engines_this_build_runs(
    tmp_path: Path,
) -> None:
    """Both engines are wired, so the swap is a line of the configuration file.

    The same file, the same model, the same agent -- one word changed -- and
    the deployment starts either way. That is the whole of what changing an
    agent's engine costs (``docs/specs/agents.md``): the transcript is the
    platform's whichever engine wrote it (ADR 0002), and a conversation's
    memory stays with the engine that wrote it (ADR 0005).
    """
    text = WITH_AGENTS.replace('engine = "langgraph"', 'engine = "pydantic-ai"')

    deployment = with_agents(tmp_path, text)

    await deployment.open()
    try:
        assert deployment.turns is not None
        assert deployment.turns.agents[0].engine is Engine.PYDANTIC_AI
    finally:
        await deployment.aclose()
    assert WIRED_ENGINES == frozenset(Engine)


def test_a_provider_of_a_kind_this_build_cannot_reach_stops_the_start_up(
    tmp_path: Path,
) -> None:
    # A client whose dependency tree fails the licence policy is a provider
    # this build does not offer (DEPENDENCIES.md).
    text = WITH_AGENTS.replace('kind = "anthropic"', 'kind = "openai"')

    with pytest.raises(ConfigError) as raised:
        with_agents(tmp_path, text)

    assert "this build cannot reach" in raised.value.problems[0]


def test_both_halves_of_the_file_report_their_problems_together(tmp_path: Path) -> None:
    # One file, two parsers, one refusal: an operator with a mistake in each
    # half fixes a deployment in one pass.
    text = WITH_AGENTS.replace("public_url =", "pulic_url =").replace(
        'model = "sonnet"', 'model = "opus"'
    )

    with pytest.raises(ConfigError) as raised:
        with_agents(tmp_path, text)

    assert "unknown key 'pulic_url'" in raised.value.problems
    assert "agents.assistant.model: 'opus' is not one of [models]" in raised.value.problems


def test_an_engine_handed_in_is_the_engine_the_configuration_is_judged_against(
    tmp_path: Path,
) -> None:
    # A test that wires a scripted engine is the deployment's whole answer to
    # "which engines are there", so an agent on that engine is accepted and
    # one on the engine this build happens to construct is not.
    text = WITH_AGENTS.replace('engine = "langgraph"', 'engine = "pydantic-ai"')
    engines = {Engine.PYDANTIC_AI: ScriptedAgent(*says("Answered."))}

    deployment = with_agents(tmp_path, text, engines=engines)

    assert deployment is not None
    with pytest.raises(ConfigError) as raised:
        with_agents(tmp_path, WITH_AGENTS, engines=engines)
    # And the refusal says what this deployment does run, which is what an
    # operator needs and is not the same list in every deployment.
    assert list(raised.value.problems) == [
        "agents.assistant.engine: the langgraph engine is not wired in this deployment;"
        " set engine to one of pydantic-ai"
    ]


# One file, two halves, and which halves each mode reads.


MODELS_ONLY = MODEL_TABLES
"""A configuration file with no sign-in in it at all: the local mode's."""

LOCAL_HOST = "127.0.0.1"


def developed(tmp_path: Path, text: str | None = None, **changes: object) -> Deployment:
    """A deployment in the local development mode, with or without a file."""
    here = tmp_path / "developed"
    here.mkdir(exist_ok=True)
    fields: dict[str, object] = {
        "local_development_host": LOCAL_HOST,
        "config_path": None if text is None else written(here, text),
        "database_url": DATABASE_URL,
        "secret_for": reading(BOTH_KEYS),
        "credentials": MemoryCredentialStore(),
        "conversation_store": MemoryConversationStore(),
        "clock": FakeClock(),
    }
    fields.update(changes)
    return Deployment.configured(**fields)  # type: ignore[arg-type]


@asyncio_test
async def test_the_local_development_mode_with_no_file_has_no_agents(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO):
        deployment = developed(tmp_path)

    await deployment.open()
    try:
        assert deployment.turns is not None
        assert deployment.turns.agents == ()
        assert deployment.local_access is not None
    finally:
        await deployment.aclose()
    assert any("no [agents] table" in record.message for record in caplog.records)


@asyncio_test
async def test_the_local_development_mode_may_be_given_the_model_tables(
    tmp_path: Path,
) -> None:
    # The chat is developed in this mode (docs/specs/frontend.md), and a chat
    # needs an agent: the mode has no sign-in, not no configuration.
    deployment = developed(tmp_path, MODELS_ONLY)

    await deployment.open()
    try:
        assert deployment.turns is not None
        assert [definition.id for definition in deployment.turns.agents] == ["assistant"]
        assert deployment.turns.agents[0].engine is Engine.LANGGRAPH
        # Still the local mode: nobody signs in, and one user is everybody.
        assert deployment.sign_in is None
        assert deployment.local_access is not None
    finally:
        await deployment.aclose()


def test_the_local_development_mode_checks_the_model_keys_the_same_way(
    tmp_path: Path,
) -> None:
    with pytest.raises(ConfigError) as raised:
        developed(tmp_path, MODELS_ONLY, secret_for=reading({}))

    assert list(raised.value.problems) == [
        "model_providers.anthropic: the API key is read from the environment variable"
        " ROBINAUTS_ANTHROPIC_KEY, which is unset or empty"
    ]


def test_the_local_development_mode_refuses_a_file_that_holds_sign_in(
    tmp_path: Path,
) -> None:
    # The mode exists where there is nothing to sign in to, so sign-in in its
    # file is a file written for another deployment.
    with pytest.raises(ConfigError) as raised:
        developed(tmp_path, WITH_AGENTS)

    assert any("cannot be combined" in problem for problem in raised.value.problems)


@pytest.mark.parametrize("table", ['public_url = "https://x.example"', "[[admin]]\nprovider = 'g'"])
def test_any_sign_in_table_at_all_is_what_that_refusal_looks_for(
    tmp_path: Path, table: str
) -> None:
    # Including `admin`, which only the sign-in parser refuses by name and
    # which nothing would read in this mode.
    with pytest.raises(ConfigError) as raised:
        developed(tmp_path, f"{table}\n{MODELS_ONLY}")

    assert any("cannot be combined" in problem for problem in raised.value.problems)


def test_a_misspelt_table_is_still_refused_in_the_local_development_mode(
    tmp_path: Path,
) -> None:
    # Reading only one half is not reading it loosely.
    with pytest.raises(ConfigError) as raised:
        developed(tmp_path, MODELS_ONLY.replace("[models.sonnet]", "[modles.sonnet]"))

    assert "unknown key 'modles'" in raised.value.problems


# The variable that names the file, and the name it used to have.


def test_the_old_variable_still_names_the_file_and_says_so(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # A deployment written against ROBINAUTS_AUTH_CONFIG goes on starting, and
    # is told once to rename the variable. An alias that worked silently is an
    # alias nobody ever renames.
    path = written(tmp_path)

    with caplog.at_level(logging.WARNING):
        deployment = Deployment.configured(
            secret_for=reading(
                {
                    AUTH_CONFIG_VARIABLE: str(path),
                    DATABASE_URL_VARIABLE: DATABASE_URL,
                    "ROBINAUTS_GOOGLE_SECRET": "a-secret",
                }
            ),
        )

    assert deployment.config is not None
    assert deployment.config.public_url == "https://robinauts.example.com"
    warned = [record.getMessage() for record in caplog.records if record.levelno >= logging.WARNING]
    assert any(AUTH_CONFIG_VARIABLE in message and CONFIG_VARIABLE in message for message in warned)


def test_the_new_variable_wins_when_both_are_set_and_the_old_one_is_ignored(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # Two names for one file are two files waiting to disagree, so which one
    # was read is said out loud.
    other = tmp_path / "other"
    other.mkdir()
    wanted = written(
        tmp_path,
        CONFIGURATION.replace("https://robinauts.example.com", "https://wanted.example.com"),
    )
    ignored = written(other, CONFIGURATION)

    with caplog.at_level(logging.WARNING):
        deployment = Deployment.configured(
            secret_for=reading(
                {
                    CONFIG_VARIABLE: str(wanted),
                    AUTH_CONFIG_VARIABLE: str(ignored),
                    DATABASE_URL_VARIABLE: DATABASE_URL,
                    "ROBINAUTS_GOOGLE_SECRET": "a-secret",
                }
            ),
        )

    assert deployment.config is not None
    assert deployment.config.public_url == "https://wanted.example.com"
    assert any("is ignored" in record.getMessage() for record in caplog.records)


def test_a_deployment_named_no_file_at_all_is_not_warned_about_the_old_name(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING), pytest.raises(ConfigError):
        Deployment.configured(secret_for=reading({}))

    assert not [record for record in caplog.records if AUTH_CONFIG_VARIABLE in record.getMessage()]


# Agents handed in, and the engines that must be able to run them.


def test_an_agent_handed_in_must_run_on_a_model_the_configuration_has(
    tmp_path: Path,
) -> None:
    # Handing in agents and not engines means they will be run by the engine
    # built from the configuration file, so a model that is not in it is a
    # turn that would fail in the middle -- the engine looking a model up and
    # finding nothing. Said at start-up instead.
    definition = agent_definition(id="stranger", model="opus", engine=Engine.LANGGRAPH)

    with pytest.raises(ConfigError) as raised:
        with_agents(tmp_path, agents={definition.id: definition})

    (problem,) = raised.value.problems
    assert problem.startswith("the agent 'stranger' that was handed in runs on model 'opus'")
    assert "hand in the engine" in problem


NO_AGENT_TABLE = CONFIGURATION + MODEL_TABLES.partition("[agents.assistant]")[0]
"""Both halves of the file, with the provider and the model but no agent."""


def test_an_agent_handed_in_with_its_own_engine_still_starts_on_a_model_on_offer(
    tmp_path: Path,
) -> None:
    # The engine that was handed in is what will run it, and what it can reach
    # is that engine's affair. What a conversation may run on is not: a turn
    # on a model the deployment does not offer is refused, so an agent whose
    # default is not among them would begin conversations that refuse their
    # first turn. Said at start-up instead.
    definition = agent_definition(id="stranger", model="opus", engine=Engine.PYDANTIC_AI)

    with pytest.raises(ConfigError) as raised:
        with_agents(
            tmp_path,
            NO_AGENT_TABLE,
            agents={definition.id: definition},
            engines={Engine.PYDANTIC_AI: ScriptedAgent(*says("Answered."))},
        )

    (problem,) = raised.value.problems
    assert problem.startswith(
        "the agent 'stranger' starts its conversations on model 'opus', which this"
        " deployment does not offer"
    )


def test_an_agent_handed_in_with_its_own_engine_may_bring_its_models(
    tmp_path: Path,
) -> None:
    # Handed in beside it, the models are what the deployment offers, and the
    # file's are not consulted.
    definition = agent_definition(id="stranger", model="opus", engine=Engine.PYDANTIC_AI)

    deployment = with_agents(
        tmp_path,
        NO_AGENT_TABLE,
        agents={definition.id: definition},
        models={"opus": model_config("opus")},
        engines={Engine.PYDANTIC_AI: ScriptedAgent(*says("Answered."))},
    )

    assert deployment is not None


def test_models_handed_in_without_engines_are_refused(tmp_path: Path) -> None:
    # The engines built from the file reach the file's models and no others,
    # so a model handed in beside them would be offered and then fail in the
    # middle of a turn.
    with pytest.raises(ConfigError) as raised:
        with_agents(tmp_path, models={"opus": model_config("opus")})

    (problem,) = raised.value.problems
    assert problem.startswith("models were handed in without the engines to run them")


def test_an_agent_handed_in_that_the_configuration_does_know_is_accepted(
    tmp_path: Path,
) -> None:
    definition = agent_definition(id="assistant", model="sonnet", engine=Engine.LANGGRAPH)

    deployment = with_agents(tmp_path, agents={definition.id: definition})

    assert deployment is not None


def test_an_agent_handed_in_must_run_on_an_engine_this_build_constructs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Handed in without an engine, and asking for one this build does not
    # make: a `ConfigError` at start-up, beside every other problem, rather
    # than the `InvalidValueError` the run lifecycle would raise at `open`.
    #
    # This build constructs every engine the vocabulary has, so the situation
    # has to be made: an `ENGINES` with one entry is what this deployment was
    # one step ago, and what it is again the day a third engine is named in the
    # configuration before its adapter exists.
    monkeypatch.delitem(app_module.ENGINES, Engine.PYDANTIC_AI)
    definition = agent_definition(id="stranger", model="sonnet", engine=Engine.PYDANTIC_AI)

    with pytest.raises(ConfigError) as raised:
        with_agents(tmp_path, NO_AGENT_TABLE, agents={definition.id: definition})

    (problem,) = raised.value.problems
    assert problem.startswith(
        "the agent 'stranger' that was handed in runs on the pydantic-ai engine"
    )
    assert "hand in the engine" in problem
