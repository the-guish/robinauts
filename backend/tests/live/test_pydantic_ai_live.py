# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""One real turn against a real provider. Run by hand, never by CI.

Everything else about this engine is proved without a network
(``tests/unit/test_pydantic_ai_engine.py``); what only this can show is that
the client is built the way the vendor expects and that a real stream maps onto
the platform's events. It costs money and needs a key, so:

- it is **not collected** by a plain ``pytest`` run, because ``tests/live`` is
  in ``norecursedirs`` (``backend/pyproject.toml``). CI runs
  ``scripts/check-tests.sh``, which is a plain run, so CI never sees it;
- it is run by naming it::

      ROBINAUTS_LIVE_ANTHROPIC_KEY=sk-... \\
          uv run pytest tests/live/test_pydantic_ai_live.py

- and even then it **skips** without the key, so naming it by mistake costs
  nothing.

The key is read from a variable of its own rather than from the one a
deployment uses, so that running the suite on a machine that has a deployment
configured does not quietly start spending its key. It is the **same** variable
the LangGraph live test reads: it is one key for one vendor, and running both
files is then one export.

**Anthropic's own endpoint only**, for the same reasons as the other engine:
the ``anthropic-compatible`` route is covered without a key by
``tests/live/test_vendor_routing.py``, and ``openai`` and
``openai-compatible`` need ``pydantic-ai-slim[openai]``, whose tree this build
does not have (``DEPENDENCIES.md``, "Known exclusions").
"""

from __future__ import annotations

import os

import pytest

from aio import asyncio_test
from conversations import agent_definition
from robinauts.adapters import ProviderKeys
from robinauts.adapters.agents.pydantic_ai import PydanticAIAgent
from robinauts.core import check_backend_events
from robinauts.domain import (
    Done,
    Engine,
    Event,
    ModelConfig,
    ModelProviderConfig,
    ModelsConfig,
    ProviderKind,
    TextDelta,
)

pytestmark = [pytest.mark.io, pytest.mark.live]

ANTHROPIC_KEY_VARIABLE = "ROBINAUTS_LIVE_ANTHROPIC_KEY"
"""The key this test spends, and nothing else in the repository reads."""

ANTHROPIC_MODEL_VARIABLE = "ROBINAUTS_LIVE_ANTHROPIC_MODEL"
"""Which model to ask, for when the default has been retired."""

DEFAULT_MODEL = "claude-haiku-4-5"
"""The cheapest model that can answer the question below."""

PROVIDER = "anthropic"
MODEL = "live"
AGENT = "live"

ASKED = "Reply with the single word: robinaut"
"""Short, deterministic enough to assert on, and a handful of tokens."""

WANTED = "robinaut"

TURN_SECONDS = 60.0
"""Generous: a slow provider is not a failure of this test."""

MAX_TOKENS = 64
"""A ceiling, so that a model that misreads the question costs nothing much."""


def live_key() -> str:
    key = os.environ.get(ANTHROPIC_KEY_VARIABLE)
    if not key:
        pytest.skip(f"set {ANTHROPIC_KEY_VARIABLE} to run one real turn against Anthropic")
    return key


def live_models() -> ModelsConfig:
    return ModelsConfig(
        providers={
            PROVIDER: ModelProviderConfig(
                id=PROVIDER, kind=ProviderKind.ANTHROPIC, api_key_env=ANTHROPIC_KEY_VARIABLE
            )
        },
        models={
            MODEL: ModelConfig(
                id=MODEL,
                provider=PROVIDER,
                name=os.environ.get(ANTHROPIC_MODEL_VARIABLE) or DEFAULT_MODEL,
                timeout_seconds=TURN_SECONDS,
                max_output_tokens=MAX_TOKENS,
            )
        },
        agents={
            AGENT: agent_definition(
                id=AGENT,
                model=MODEL,
                engine=Engine.PYDANTIC_AI,
                system_prompt="Answer in one word, in lower case, with no punctuation.",
            )
        },
    )


@asyncio_test
async def test_one_real_turn_against_anthropic_streams_and_is_done() -> None:
    models = live_models()
    agent = PydanticAIAgent(models, ProviderKeys({PROVIDER: live_key()}))
    seen: list[Event] = []

    async for event in agent.stream(models.agents[AGENT], ASKED, model=MODEL, state=None):
        seen.append(event)

    check_backend_events(seen)
    assert [event for event in seen if isinstance(event, TextDelta)]
    done = seen[-1]
    assert isinstance(done, Done)
    assert WANTED in done.text.lower()
    # The memory came back with it, in the framework's own format.
    assert isinstance(done.state, bytes) and done.state
    assert agent.held == 0
