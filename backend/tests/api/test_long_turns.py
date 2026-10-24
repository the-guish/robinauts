# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""A long turn through each engine: many tool rounds, and a vendor's error on the way."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from util.controller_db import requires_postgres
from util.fake_openai import CallTool, FakeLocalGPTServer, FakeModel, Overloaded

pytestmark = requires_postgres

ROUNDS = 50


class LongTaskModel:
    """Calls ``shout`` 50 times, one call per request, then answers "done". Its third request
    fails once with a 503, as a vendor's hiccup."""

    def __init__(self) -> None:
        self.failed = False

    def reply(self, messages: list[dict[str, Any]]) -> str | CallTool:
        rounds = sum(m["role"] == "tool" for m in messages)
        if rounds == 2 and not self.failed:
            self.failed = True
            raise Overloaded("overloaded")
        if rounds < ROUNDS:
            return CallTool("shout", {"text": str(rounds)}, call_id=f"call_{rounds}")
        return "done"


@pytest.fixture
def fake_model() -> FakeModel:
    return LongTaskModel()


@pytest.mark.parametrize("agent", ["langchain_tools", "pydantic_ai_tools", "kataloop_tools"])
def test_a_turn_outlasts_a_vendor_error_and_fifty_tool_rounds(
    api: httpx.Client, local_gpt: FakeLocalGPTServer, agent: str
) -> None:
    started = api.post("/api/turns", json={"agent_id": agent, "text": "go"}, timeout=120.0)
    assert started.status_code == 200, started.text
    last = [line for line in started.text.splitlines() if line.startswith("data: ")][-1]
    assert '"RUN_FINISHED"' in last
    # 51 requests for 50 rounds and the answer, and the one that failed.
    assert len(local_gpt.received) == ROUNDS + 2
