# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""One real turn against a real provider. Run by hand, never by CI.

Everything else about this engine is proved without a network
(``tests/unit/test_langgraph_engine.py``); what only this can show is that the
client is built the way the vendor expects and that a real stream maps onto
the platform's events. It costs money and needs a key, so:

- it is **not collected** by a plain ``pytest`` run, because ``tests/live`` is
  in ``norecursedirs`` (``backend/pyproject.toml``). CI runs
  ``scripts/check-tests.sh``, which is a plain run, so CI never sees it;
- it is run by naming it::

      ROBINAUTS_LIVE_ANTHROPIC_KEY=sk-... \\
          uv run pytest tests/live/test_langgraph_live.py

- and even then it **skips** without the key, so naming it by mistake costs
  nothing.

The key is read from a variable of its own rather than from the one a
deployment uses, so that running the suite on a machine that has a deployment
configured does not quietly start spending its key.

What the turn is -- the variables, the models asked by default, the question
-- is shared with the other engine's live test (``tests/live_turns.py``).

**The two vendors' own endpoints**, one turn each, each skipped without its
own key: Anthropic's with ``ROBINAUTS_LIVE_ANTHROPIC_KEY``, OpenAI's with
``ROBINAUTS_LIVE_OPENAI_KEY``::

    ROBINAUTS_LIVE_OPENAI_KEY=sk-... \\
        uv run pytest tests/live/test_langgraph_live.py

The engine also reaches the two ``-compatible`` kinds -- any endpoint that
speaks one of the two protocols at a configured ``base_url``, OpenRouter's
among them -- and those routes have a test of their own that needs no key at
all (``tests/live/test_vendor_routing.py``), so there is nothing here to
repeat.
"""

from __future__ import annotations

import pytest

import live_turns
from aio import asyncio_test
from robinauts.legacy.adapters.agents.langgraph import LangGraphAgent
from robinauts.legacy.domain import Engine

pytestmark = [pytest.mark.io, pytest.mark.live]


@asyncio_test
async def test_one_real_turn_against_anthropic_streams_and_completes() -> None:
    models = live_turns.anthropic_models(Engine.LANGGRAPH)
    agent = LangGraphAgent(models, live_turns.anthropic_keys())

    await live_turns.one_real_turn(agent, models)

    assert agent.held == 0


@asyncio_test
async def test_one_real_turn_against_openai_streams_and_completes() -> None:
    """The same turn over Chat Completions, at OpenAI's own endpoint."""
    models = live_turns.openai_models(Engine.LANGGRAPH)
    agent = LangGraphAgent(models, live_turns.openai_keys())

    await live_turns.one_real_turn(agent, models)

    assert agent.held == 0
