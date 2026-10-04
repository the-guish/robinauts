# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""One real turn of each engine per route. Run by hand, never by CI.

``tests/adapters_metered`` is not collected by a plain run (``norecursedirs``); name the file
to run it.
Each test reads its key from a variable of its own and skips without it. Every test runs once
per engine (the engine is in the test's id).
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable

import pytest

from robinauts.agent_engines.contract.domain import (
    AgentDefinition,
    Done,
    ModelConfig,
    ModelProviderConfig,
    ModelsConfig,
    ProviderKind,
    TextDelta,
)
from robinauts.agent_engines.contract.ports import AgentEngine, EngineSettings, ProviderKeyLookup
from robinauts.agent_engines.langchain_engine.engine import LangChainEngine
from robinauts.agent_engines.langchain_engine.memory import InProcessMemory as LangChainMemory
from robinauts.agent_engines.pydantic_ai_engine.engine import PydanticAIEngine
from robinauts.agent_engines.pydantic_ai_engine.memory import InProcessMemory as PydanticAIMemory
from util.aio import asyncio_test
from util.engine_settings import NoSecrets

pytestmark = [pytest.mark.io, pytest.mark.live]

ANTHROPIC_KEY = "ROBINAUTS_LIVE_ANTHROPIC_KEY"
OPENROUTER_KEY = "ROBINAUTS_LIVE_OPENROUTER_KEY"


def langchain(settings: EngineSettings) -> AgentEngine:
    return LangChainEngine(settings, LangChainMemory())


def pydantic_ai(settings: EngineSettings) -> AgentEngine:
    return PydanticAIEngine(settings, PydanticAIMemory())


NewEngine = Callable[[EngineSettings], AgentEngine]

ENGINES = pytest.mark.parametrize(
    "new_engine", [langchain, pydantic_ai], ids=["langchain", "pydantic-ai"]
)


class Key(ProviderKeyLookup):
    def __init__(self, key: str) -> None:
        self._key = key

    def key_for(self, provider_id: str) -> str:
        return self._key


async def one_real_turn(
    new_engine: NewEngine, provider: ModelProviderConfig, name: str, key: str
) -> None:
    model = ModelConfig(
        id="m", provider=provider.id, name=name, timeout_seconds=60.0, max_output_tokens=64
    )
    settings = EngineSettings(
        models=ModelsConfig(providers={provider.id: provider}, models={"m": model}),
        keys=Key(key),
        tool_secrets=NoSecrets(),
        max_model_calls_per_turn=200,
    )
    engine = new_engine(settings)
    await engine.setup()
    session = uuid.uuid4()
    await engine.create(session)
    agent = AgentDefinition(system_prompt="You answer exactly as asked.")
    events = [
        event
        async for event in engine.stream(
            session,
            agent,
            "Reply with the single word: robinaut",
            model="m",
            checkpoint_id=None,
            timeout_seconds=60.0,
        )
    ]
    assert any(isinstance(event, TextDelta) for event in events)
    assert [type(event) for event in events].count(Done) == 1
    done = events[-1]
    assert isinstance(done, Done)
    assert "robinaut" in done.text.lower()


@pytest.mark.skipif(not os.environ.get(ANTHROPIC_KEY), reason=f"{ANTHROPIC_KEY} is not set")
@ENGINES
@asyncio_test
async def test_one_real_turn_on_anthropic(new_engine: NewEngine) -> None:
    provider = ModelProviderConfig(id="anthropic", kind=ProviderKind.ANTHROPIC, api_key_env="")
    name = os.environ.get("ROBINAUTS_LIVE_ANTHROPIC_MODEL", "claude-haiku-4-5")
    await one_real_turn(new_engine, provider, name, os.environ[ANTHROPIC_KEY])


@pytest.mark.skipif(not os.environ.get(OPENROUTER_KEY), reason=f"{OPENROUTER_KEY} is not set")
@ENGINES
@asyncio_test
async def test_one_real_turn_through_openrouter(new_engine: NewEngine) -> None:
    provider = ModelProviderConfig(
        id="openrouter",
        kind=ProviderKind.OPENAI_COMPATIBLE,
        api_key_env="",
        base_url="https://openrouter.ai/api/v1",
    )
    name = os.environ.get("ROBINAUTS_LIVE_OPENROUTER_MODEL", "anthropic/claude-haiku-4.5")
    await one_real_turn(new_engine, provider, name, os.environ[OPENROUTER_KEY])
