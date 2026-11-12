# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""An agent's ``turn_timeout_seconds`` ends its turns; other agents keep ``[work]``'s."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from util import stack
from util.controller_db import requires_postgres
from util.fake_openai import EchoModel, FakeLocalGPTServer, FakeModel

pytestmark = requires_postgres

HASTY = f"""
[agents.hasty]
title = "Hasty"
system_prompt = "{stack.SYSTEM_PROMPT}"
models = ["local_gpt"]
engine = "pydantic-ai"
turn_timeout_seconds = 1
"""


class SlowEchoModel(EchoModel):
    """An echo that takes two seconds to answer."""

    def reply(self, messages: list[dict[str, Any]]) -> str:
        time.sleep(2)
        return super().reply(messages)


@pytest.fixture
def fake_model() -> FakeModel:
    return SlowEchoModel()


def last_event(api: httpx.Client, agent: str) -> str:
    started = api.post("/api/turns", json={"agent_id": agent, "text": "slow"})
    assert started.status_code == 200, started.text
    return [line for line in started.text.splitlines() if line.startswith("data: ")][-1]


def test_only_the_agent_with_a_short_timeout_runs_out_of_time(
    local_gpt: FakeLocalGPTServer, tmp_path: Path
) -> None:
    config = tmp_path / "robinauts.toml"
    config.write_text(stack.config_for(local_gpt.base_url) + HASTY)
    with (
        stack.database(config) as url,
        stack.server(config, url, env=stack.API_KEY) as server,
        httpx.Client(base_url=server, timeout=30.0) as api,
    ):
        assert '"RUN_ERROR"' in last_event(api, "hasty")
        assert '"RUN_FINISHED"' in last_event(api, "pydantic_ai")
