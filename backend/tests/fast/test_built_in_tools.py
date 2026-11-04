# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""This build's tools, through the controller: an agent that names one is handed its function
tools for the turn, bound to the turn's owner and agent.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncGenerator
from typing import Any

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
from robinauts.controller.composition import compose
from robinauts.controller.contract.domain import (
    AgentConfig,
    Config,
    Identity,
    ModelConfig,
    ProviderConfig,
    ProviderKind,
    StorageConfig,
    StorageKind,
    TurnStarted,
    User,
)
from robinauts.web.lifecycle import Lifecycle
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


async def started_over(engine: SearchingEngine) -> Lifecycle:
    composed = compose(
        CONFIG,
        storage=StorageConfig(StorageKind.IN_MEMORY),
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
