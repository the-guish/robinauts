# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""This build's tools, through the controller: an agent that names one is handed its function
tools for the turn, bound to the turn's owner and agent: the sessions it searches, and the
folder it keeps files in.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import pytest
from echo_controller import wait_for_turn_end

from robinauts.agent_engines.contract.domain import (
    AgentDefinition,
    Done,
    Event,
    TextDelta,
    ToolCall,
    ToolResult,
)
from robinauts.agent_engines.echo_engine.engine import EchoEngine
from robinauts.cli.composition import compose, configure, file_system_for
from robinauts.cli.lifecycle import Lifecycle
from robinauts.controller.contract.domain import ConfigError, Identity, User
from robinauts.shared_domain.domain import (
    AgentConfig,
    Config,
    FilesConfig,
    FilesUnusableError,
    ModelConfig,
    ProviderConfig,
    ProviderKind,
    StorageConfig,
    StorageKind,
    TurnStarted,
)
from util.aio import asyncio_test

SEARCH = "search: "

CONFIG = Config(
    providers={"echo": ProviderConfig("echo", ProviderKind.ANTHROPIC, "ECHO_API_KEY")},
    models={"echo": ModelConfig("echo", provider="echo", name="echo", title="Echo")},
    agents={
        "keeper": AgentConfig(
            "keeper", "Keeper", system_prompt="", model="echo", engine="echo", tools=("sessions",)
        ),
        "other": AgentConfig("other", "Other", system_prompt="", model="echo", engine="echo"),
    },
)


class SearchingEngine(EchoEngine):
    """The echo engine, which on a prompt starting with ``SEARCH`` calls the search it was
    handed with the rest, as a model would, and answers what it said."""

    async def stream(
        self, session_id: uuid.UUID, agent: AgentDefinition, prompt: str, **options: Any
    ) -> AsyncGenerator[Event, None]:
        if not prompt.startswith(SEARCH):
            async for event in super().stream(session_id, agent, prompt, **options):
                yield event
            return
        [search] = [f for f in agent.functions if f.name == "search_sessions"]
        arguments = {"query": prompt.removeprefix(SEARCH)}
        yield ToolCall("call-1", search.name, arguments)
        output = await search.call(arguments)
        yield ToolResult("call-1", search.name, output)
        yield TextDelta(output)
        yield Done(output, checkpoint_id=str(uuid.uuid4()))


async def started_over(engine: EchoEngine, config: Config = CONFIG) -> Lifecycle:
    composed = compose(
        config,
        storage=StorageConfig(StorageKind.IN_PROCESS),
        secret_for={}.get,
        engines={"echo": lambda *_: engine},
    )
    lifecycle = Lifecycle(composed)
    await lifecycle.start()
    return lifecycle


async def talk(controller: Any, user: User, agent: str, text: str) -> TurnStarted:
    started = await controller.start_session(user, agent=agent, model="echo", text=text)
    await wait_for_turn_end(controller, user, started)
    return started


@asyncio_test
async def test_the_search_finds_the_owners_sessions_with_the_same_agent_alone() -> None:
    engine = SearchingEngine()
    lifecycle = await started_over(engine)
    controller: Any = lifecycle.composed.controller
    me = await controller.ensure_user(Identity("local", "me"))
    someone = await controller.ensure_user(Identity("local", "someone"))
    mine = await talk(controller, me, "keeper", "the zebra plan")
    await talk(controller, me, "other", "the zebra plan, told another agent")
    await talk(controller, someone, "keeper", "the zebra plan of someone else")

    asked = await talk(controller, me, "keeper", SEARCH + "zebra plan")

    answer = (await controller.open_session(me, asked.session_id)).messages[-1]
    found = json.loads(answer.parts[-1].text)
    assert [c["session_id"] for c in found["sessions"]] == [str(mine.session_id)]
    assert [m["excerpt"] for m in found["sessions"][0]["matches"]] == [
        "the zebra plan",
        "The tool said: the zebra plan",
    ]
    await lifecycle.stop()


# --- the folder -----------------------------------------------------------------------

NOTE = "note: "
RECALL = "recall"
NOTED = "noted"
NOTES = "notes.md"


def noting_config(files: Path) -> Config:
    """Two agents that keep files, one that does not, on a files root of the test's."""
    return Config(
        providers=CONFIG.providers,
        models=CONFIG.models,
        agents={
            "keeper": AgentConfig(
                "keeper",
                "Keeper",
                system_prompt="",
                model="echo",
                engine="echo",
                tools=("sessions", "basic_edit"),
            ),
            "other": AgentConfig(
                "other",
                "Other",
                system_prompt="",
                model="echo",
                engine="echo",
                tools=("basic_edit",),
            ),
            "plain": AgentConfig("plain", "Plain", system_prompt="", model="echo", engine="echo"),
        },
        files=FilesConfig(path=str(files)),
    )


class NotingEngine(EchoEngine):
    """The echo engine, which on ``note: <text>`` writes the text to ``notes.md`` with the
    ``write_file`` it was handed and answers ``noted``, and on ``recall`` reads it back and
    answers its content, or what the tool said when it could not."""

    async def stream(
        self, session_id: uuid.UUID, agent: AgentDefinition, prompt: str, **options: Any
    ) -> AsyncGenerator[Event, None]:
        functions = {f.name: f for f in agent.functions}
        if prompt.startswith(NOTE):
            arguments: dict[str, Any] = {"path": NOTES, "content": prompt.removeprefix(NOTE)}
            yield ToolCall("call-1", "write_file", arguments)
            yield ToolResult("call-1", "write_file", await functions["write_file"].call(arguments))
            yield TextDelta(NOTED)
            yield Done(NOTED, checkpoint_id=str(uuid.uuid4()))
        elif prompt == RECALL:
            arguments = {"path": NOTES}
            yield ToolCall("call-1", "read_file", arguments)
            try:
                output = await functions["read_file"].call(arguments)
            except Exception as refused:  # noqa: BLE001 -- as an engine tells the model
                answer = str(refused)
                yield ToolResult("call-1", "read_file", answer, failed=True)
            else:
                answer = json.loads(output)["content"]
                yield ToolResult("call-1", "read_file", output)
            yield TextDelta(answer)
            yield Done(answer, checkpoint_id=str(uuid.uuid4()))
        else:
            async for event in super().stream(session_id, agent, prompt, **options):
                yield event


async def answered(controller: Any, user: User, agent: str, text: str) -> str:
    started = await talk(controller, user, agent, text)
    answer = (await controller.open_session(user, started.session_id)).messages[-1]
    return answer.parts[-1].text


@asyncio_test
async def test_each_owner_and_agent_keeps_files_in_a_folder_of_their_own(tmp_path: Path) -> None:
    root = tmp_path / "files"
    lifecycle = await started_over(NotingEngine(), noting_config(root))
    controller: Any = lifecycle.composed.controller
    me = await controller.ensure_user(Identity("local", "me"))
    someone = await controller.ensure_user(Identity("local", "someone"))
    assert await answered(controller, me, "keeper", NOTE + "mine, with keeper") == NOTED
    assert await answered(controller, someone, "keeper", NOTE + "theirs, with keeper") == NOTED
    assert await answered(controller, me, "other", NOTE + "mine, with other") == NOTED

    # Each reads back its own, in a new conversation.
    assert await answered(controller, me, "keeper", RECALL) == "mine, with keeper"
    assert await answered(controller, someone, "keeper", RECALL) == "theirs, with keeper"
    assert await answered(controller, me, "other", RECALL) == "mine, with other"

    # Where they are: <root>/<owner>/<agent>, and the root was made at the start.
    assert (root / str(me.id) / "keeper" / NOTES).read_text() == "mine, with keeper"
    assert (root / str(someone.id) / "keeper" / NOTES).read_text() == "theirs, with keeper"
    assert (root / str(me.id) / "other" / NOTES).read_text() == "mine, with other"
    assert sorted(p.name for p in root.iterdir()) == sorted([str(me.id), str(someone.id)])
    await lifecycle.stop()


@asyncio_test
async def test_a_root_that_cannot_be_used_stops_the_start(tmp_path: Path) -> None:
    a_file = tmp_path / "a-file"
    a_file.write_text("x")
    composed = compose(
        noting_config(a_file),
        storage=StorageConfig(StorageKind.IN_PROCESS),
        secret_for={}.get,
        engines={"echo": lambda *_: NotingEngine()},
    )
    with pytest.raises(FilesUnusableError, match="cannot be made"):
        await Lifecycle(composed).start()


def test_no_file_system_is_built_when_no_agent_keeps_files() -> None:
    assert file_system_for(CONFIG) is None
    assert file_system_for(noting_config(Path("/tmp/x"))) is not None


def test_an_agent_that_keeps_files_needs_an_id_that_can_name_a_folder() -> None:
    tables: dict[str, Any] = {
        "model_providers": {"echo": {"kind": "anthropic", "api_key_env": "ECHO_API_KEY"}},
        "models": {"echo": {"provider": "echo", "name": "echo"}},
        "agents": {
            "a/b": {"title": "A", "system_prompt": "", "model": "echo", "engine": "echo"},
            "..": {"title": "Dots", "system_prompt": "", "model": "echo", "engine": "echo"},
            "fine": {
                "title": "Fine",
                "system_prompt": "",
                "model": "echo",
                "engine": "echo",
                "tools": ["basic_edit"],
            },
        },
    }
    # An odd id is fine for an agent that keeps no files.
    configure(tables, {})
    tables["agents"]["a/b"]["tools"] = ["basic_edit"]
    with pytest.raises(ConfigError) as refused:
        configure(tables, {})
    assert str(refused.value).startswith(
        "agents.a/b: an agent with the basic_edit tool keeps files in a folder named after it,"
        " and 'a/b' cannot name one"
    )
    assert "agents.fine" not in str(refused.value)
    assert "agents...:" not in str(refused.value)
