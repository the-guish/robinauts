# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""One real turn against a real provider, as both engines' live tests run it.

What ``tests/live/test_langgraph_live.py`` and
``tests/live/test_pydantic_ai_live.py`` share: the variables a key is read
from, the models asked by default, the question and the one turn. Each of those
modules builds its own engine and says why it is run and how; this names no
engine, so that deleting an adapter breaks its own live test and nothing else
(``docs/layout.md``).

**Nothing here spends anything by itself.** A key is read only from a variable
of the test's own, never from one a deployment uses, and a test with no key
skips (``live_key``).
"""

from __future__ import annotations

import os

import pytest

from conversations import agent_definition
from robinauts.legacy.adapters import ProviderKeys
from robinauts.legacy.core import check_backend_events
from robinauts.legacy.domain import (
    Done,
    Engine,
    Event,
    ModelConfig,
    ModelProviderConfig,
    ModelsConfig,
    ProviderKind,
    TextDelta,
)
from robinauts.legacy.ports import Agent

ANTHROPIC_KEY_VARIABLE = "ROBINAUTS_LIVE_ANTHROPIC_KEY"
"""The Anthropic key the live tests spend, and nothing else in the repository reads."""

ANTHROPIC_MODEL_VARIABLE = "ROBINAUTS_LIVE_ANTHROPIC_MODEL"
"""Which Anthropic model to ask, for when the default has been retired."""

DEFAULT_ANTHROPIC_MODEL = "claude-haiku-4-5"
"""The cheapest Anthropic model that can answer the question below."""

OPENAI_KEY_VARIABLE = "ROBINAUTS_LIVE_OPENAI_KEY"
"""The OpenAI key the live tests spend, and nothing else in the repository reads."""

OPENAI_MODEL_VARIABLE = "ROBINAUTS_LIVE_OPENAI_MODEL"
"""Which OpenAI model to ask, for when the default has been retired."""

DEFAULT_OPENAI_MODEL = "gpt-5.4-nano"
"""A cheap model that answers the question below without reasoning first.

Chosen over a newer cheap one because it does not reason by default: on a
model that does, the ceiling below covers the reasoning as well, and could be
spent on it before a word of the answer.
"""

PROVIDER = "live"
MODEL = "live"
AGENT = "live"

ASKED = "Reply with the single word: robinaut"
"""Short, deterministic enough to assert on, and a handful of tokens."""

WANTED = "robinaut"

TURN_SECONDS = 60.0
"""Generous: a slow provider is not a failure of this test."""

MAX_TOKENS = 64
"""A ceiling, so that a model that misreads the question costs nothing much."""


def live_key(variable: str, vendor: str) -> str:
    """The key in that variable, or a skip saying which variable to set."""
    key = os.environ.get(variable)
    if not key:
        pytest.skip(f"set {variable} to run one real turn against {vendor}")
    return key


def anthropic_keys() -> ProviderKeys:
    """The live Anthropic key, as the engine is handed it, or a skip."""
    return ProviderKeys({PROVIDER: live_key(ANTHROPIC_KEY_VARIABLE, "Anthropic")})


def openai_keys() -> ProviderKeys:
    """The live OpenAI key, as the engine is handed it, or a skip."""
    return ProviderKeys({PROVIDER: live_key(OPENAI_KEY_VARIABLE, "OpenAI")})


def anthropic_models(engine: Engine) -> ModelsConfig:
    """Anthropic itself, the model to ask, and one agent on that engine."""
    name = os.environ.get(ANTHROPIC_MODEL_VARIABLE) or DEFAULT_ANTHROPIC_MODEL
    return _live_models(engine, ProviderKind.ANTHROPIC, ANTHROPIC_KEY_VARIABLE, name)


def openai_models(engine: Engine) -> ModelsConfig:
    """OpenAI itself, over Chat Completions, the model to ask, and one agent on that engine."""
    name = os.environ.get(OPENAI_MODEL_VARIABLE) or DEFAULT_OPENAI_MODEL
    return _live_models(engine, ProviderKind.OPENAI, OPENAI_KEY_VARIABLE, name)


def _live_models(engine: Engine, kind: ProviderKind, variable: str, name: str) -> ModelsConfig:
    return ModelsConfig(
        providers={PROVIDER: ModelProviderConfig(id=PROVIDER, kind=kind, api_key_env=variable)},
        models={
            MODEL: ModelConfig(
                id=MODEL,
                provider=PROVIDER,
                name=name,
                timeout_seconds=TURN_SECONDS,
                max_output_tokens=MAX_TOKENS,
            )
        },
        agents={
            AGENT: agent_definition(
                id=AGENT,
                model=MODEL,
                engine=engine,
                system_prompt="Answer in one word, in lower case, with no punctuation.",
            )
        },
    )


async def one_real_turn(agent: Agent, models: ModelsConfig) -> None:
    """Ask the question, and hold the answer to the port's order and to the word.

    That the engine holds nothing afterwards is the caller's to assert: the
    count is each engine's own (``held``), and not the port's.
    """
    seen: list[Event] = []

    async for event in agent.stream(models.agents[AGENT], ASKED, model=MODEL, state=None):
        seen.append(event)

    check_backend_events(seen)
    assert [event for event in seen if isinstance(event, TextDelta)]
    done = seen[-1]
    assert isinstance(done, Done)
    assert WANTED in done.text.lower()
    # The memory came back with it, in the framework's own format.
    assert isinstance(done.state, bytes)
    assert done.state
