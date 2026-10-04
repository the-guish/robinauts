# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""A long turn through each engine: many tool rounds, a vendor's error on the way, and the
bound on the model calls one turn may make, which ``util.stack`` sets."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from util.controller_db import requires_postgres
from util.fake_openai import CallTool, FakeLocalGPTServer, FakeModel, Overloaded
from util.stack import MAX_MODEL_CALLS_PER_TURN

pytestmark = requires_postgres

ROUNDS = 50

AGENTS = ["langchain_tools", "pydantic_ai_tools"]


class LongTaskModel:
    """Calls ``shout`` ``rounds`` times, one call per request, then answers "done". With
    ``hiccup``, its third request fails once with a 503, as a vendor's hiccup."""

    def __init__(self, rounds: int, hiccup: bool) -> None:
        self.rounds = rounds
        self.failed = not hiccup

    def reply(self, messages: list[dict[str, Any]]) -> str | CallTool:
        rounds = sum(m["role"] == "tool" for m in messages)
        if rounds == 2 and not self.failed:
            self.failed = True
            raise Overloaded("overloaded")
        if rounds < self.rounds:
            return CallTool("shout", {"text": str(rounds)}, call_id=f"call_{rounds}")
        return "done"


@pytest.fixture
def fake_model(request: pytest.FixtureRequest) -> FakeModel:
    return LongTaskModel(*request.param)


def last_event(api: httpx.Client, agent: str) -> str:
    started = api.post("/api/turns", json={"agent_id": agent, "text": "go"}, timeout=120.0)
    assert started.status_code == 200, started.text
    return [line for line in started.text.splitlines() if line.startswith("data: ")][-1]


@pytest.mark.parametrize("fake_model", [(ROUNDS, True)], indirect=True)
@pytest.mark.parametrize("agent", AGENTS)
def test_a_turn_outlasts_a_vendor_error_and_fifty_tool_rounds(
    api: httpx.Client, local_gpt: FakeLocalGPTServer, agent: str
) -> None:
    assert '"RUN_FINISHED"' in last_event(api, agent)
    # 51 requests for 50 rounds and the answer, and the one that failed.
    assert len(local_gpt.received) == ROUNDS + 2


@pytest.mark.parametrize("fake_model", [(MAX_MODEL_CALLS_PER_TURN - 1, True)], indirect=True)
@pytest.mark.parametrize("agent", AGENTS)
def test_a_turn_may_make_every_model_call_of_the_bound(
    api: httpx.Client, local_gpt: FakeLocalGPTServer, agent: str
) -> None:
    assert '"RUN_FINISHED"' in last_event(api, agent)
    # The answer is the bound's last call; the request that failed and was retried is not one.
    assert len(local_gpt.received) == MAX_MODEL_CALLS_PER_TURN + 1


@pytest.mark.parametrize("fake_model", [(MAX_MODEL_CALLS_PER_TURN, False)], indirect=True)
@pytest.mark.parametrize("agent", AGENTS)
def test_a_turn_that_needs_one_model_call_more_fails(
    api: httpx.Client, local_gpt: FakeLocalGPTServer, agent: str
) -> None:
    assert '"RUN_ERROR"' in last_event(api, agent)
    assert len(local_gpt.received) == MAX_MODEL_CALLS_PER_TURN
