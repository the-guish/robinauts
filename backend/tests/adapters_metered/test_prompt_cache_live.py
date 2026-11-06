# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The prompt cache on Anthropic's protocol, hit for real: two turns of one conversation, the
second read from the cache the first wrote. Run by hand, never by CI.

``tests/adapters_metered`` is not collected by a plain run (``norecursedirs``); name the file
to run it. Each test reads its key from a variable of its own and skips without it. Every test
runs once per engine (the engine is in the test's id). The engine says nothing of usage, so
the test reads it where the engine keeps it: in its session store, as its framework recorded the
vendor's answer.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable
from typing import Any

import pytest
from langchain_core.messages import AIMessage
from pydantic_ai.messages import ModelResponse

from robinauts.agent_engines.contract.domain import (
    AgentDefinition,
    Done,
    ModelConfig,
    ModelProviderConfig,
    ModelsConfig,
    ProviderKind,
)
from robinauts.agent_engines.contract.ports import AgentEngine, EngineSettings, ProviderKeyLookup
from robinauts.agent_engines.langchain_engine.engine import LangChainEngine
from robinauts.agent_engines.langchain_engine.session_store import (
    InProcessSessionStore as LangChainSessionStore,
)
from robinauts.agent_engines.pydantic_ai_engine.engine import PydanticAIEngine
from robinauts.agent_engines.pydantic_ai_engine.session_store import (
    InProcessSessionStore as PydanticAISessionStore,
)
from util.aio import asyncio_test
from util.engine_settings import NoSecrets

pytestmark = [pytest.mark.io, pytest.mark.live]

ANTHROPIC_KEY = "ROBINAUTS_LIVE_ANTHROPIC_KEY"
OPENROUTER_KEY = "ROBINAUTS_LIVE_OPENROUTER_KEY"

SYSTEM_PROMPT = "You answer in one short sentence.\n\n" + "\n".join(
    f"House rule {n}: a robinaut keeps note {n} of the logbook tidy, dated and signed."
    for n in range(1, 400)
)
"""About 8,000 tokens: past the 4,096 a Haiku model caches at least."""


class Key(ProviderKeyLookup):
    def __init__(self, key: str) -> None:
        self._key = key

    def key_for(self, provider_id: str) -> str:
        return self._key


async def langchain_cache_read(settings: EngineSettings) -> tuple[AgentEngine, Any]:
    sessions = LangChainSessionStore()

    async def read(session: uuid.UUID) -> int:
        """Tokens the last answer read from the cache."""
        saved = await sessions.saver.aget_tuple({"configurable": {"thread_id": str(session)}})
        assert saved is not None
        answers = [
            m for m in saved.checkpoint["channel_values"]["messages"] if isinstance(m, AIMessage)
        ]
        details = (answers[-1].usage_metadata or {}).get("input_token_details", {})
        return int(details.get("cache_read", 0))

    return LangChainEngine(settings, sessions), read


async def pydantic_ai_cache_read(settings: EngineSettings) -> tuple[AgentEngine, Any]:
    sessions = PydanticAISessionStore()

    async def read(session: uuid.UUID) -> int:
        """Tokens the last answer read from the cache."""
        latest = await sessions.latest(session)
        assert latest is not None
        answers = [m for m in latest[1] if isinstance(m, ModelResponse)]
        return answers[-1].usage.cache_read_tokens

    return PydanticAIEngine(settings, sessions), read


NewEngine = Callable[[EngineSettings], Any]

ENGINES = pytest.mark.parametrize(
    "new_engine", [langchain_cache_read, pydantic_ai_cache_read], ids=["langchain", "pydantic-ai"]
)


async def two_turns(
    new_engine: NewEngine, provider: ModelProviderConfig, name: str, key: str
) -> None:
    model = ModelConfig(
        id="m", provider=provider.id, name=name, timeout_seconds=60.0, max_output_tokens=64
    )
    settings = EngineSettings(
        models=ModelsConfig(providers={provider.id: provider}, models={"m": model}),
        keys=Key(key),
        tool_secrets=NoSecrets(),
    )
    engine, cache_read = await new_engine(settings)
    await engine.setup()
    session = uuid.uuid4()
    await engine.create(session)
    agent = AgentDefinition(system_prompt=SYSTEM_PROMPT)

    async def turn(prompt: str, after: str | None) -> Done:
        stream = engine.stream(
            session, agent, prompt, model="m", checkpoint_id=after, timeout_seconds=60.0
        )
        done = [event async for event in stream][-1]
        assert isinstance(done, Done)
        return done

    first = await turn("Which rule is about note 7?", None)
    await turn("And which one is about note 8?", first.checkpoint_id)
    # The second turn's prompt begins with the first one's, the system prompt with it.
    assert await cache_read(session) > len(SYSTEM_PROMPT) // 8


@pytest.mark.skipif(not os.environ.get(ANTHROPIC_KEY), reason=f"{ANTHROPIC_KEY} is not set")
@ENGINES
@asyncio_test
async def test_the_cache_is_read_on_anthropic(new_engine: NewEngine) -> None:
    provider = ModelProviderConfig(id="anthropic", kind=ProviderKind.ANTHROPIC, api_key_env="")
    name = os.environ.get("ROBINAUTS_LIVE_ANTHROPIC_MODEL", "claude-haiku-4-5")
    await two_turns(new_engine, provider, name, os.environ[ANTHROPIC_KEY])


@pytest.mark.skipif(not os.environ.get(OPENROUTER_KEY), reason=f"{OPENROUTER_KEY} is not set")
@ENGINES
@asyncio_test
async def test_the_cache_is_read_through_openrouter(new_engine: NewEngine) -> None:
    provider = ModelProviderConfig(
        id="openrouter",
        kind=ProviderKind.ANTHROPIC_COMPATIBLE,
        api_key_env="",
        base_url="https://openrouter.ai/api",
    )
    name = os.environ.get("ROBINAUTS_LIVE_OPENROUTER_MODEL", "anthropic/claude-haiku-4.5")
    await two_turns(new_engine, provider, name, os.environ[OPENROUTER_KEY])
