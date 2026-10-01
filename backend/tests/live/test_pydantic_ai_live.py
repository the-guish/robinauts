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

What the turn is -- the variables, the models asked by default, the question
-- is shared with the other engine's live test (``tests/live_turns.py``).

**The two vendors' own endpoints**, one turn each, each skipped without its
own key -- ``ROBINAUTS_LIVE_ANTHROPIC_KEY`` and ``ROBINAUTS_LIVE_OPENAI_KEY``,
the same two the other engine's live test reads -- for the same reasons as the
other engine: the two ``-compatible`` routes are covered without a key by
``tests/live/test_vendor_routing.py``.
"""

from __future__ import annotations

import pytest

import live_turns
from aio import asyncio_test
from robinauts.legacy.adapters.agents.pydantic_ai import PydanticAIAgent
from robinauts.legacy.domain import Engine

pytestmark = [pytest.mark.io, pytest.mark.live]


@asyncio_test
async def test_one_real_turn_against_anthropic_streams_and_completes() -> None:
    models = live_turns.anthropic_models(Engine.PYDANTIC_AI)
    agent = PydanticAIAgent(models, live_turns.anthropic_keys())

    await live_turns.one_real_turn(agent, models)

    assert agent.held == 0


@asyncio_test
async def test_one_real_turn_against_openai_streams_and_completes() -> None:
    """The same turn over Chat Completions, at OpenAI's own endpoint."""
    models = live_turns.openai_models(Engine.PYDANTIC_AI)
    agent = PydanticAIAgent(models, live_turns.openai_keys())

    await live_turns.one_real_turn(agent, models)

    assert agent.held == 0
