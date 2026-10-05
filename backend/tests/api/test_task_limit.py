# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""A worker runs at most ``max_running_tasks_per_worker`` turns; the rest wait queued."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest

from util import stack
from util.controller_db import requires_postgres
from util.fake_openai import FakeLocalGPTServer, FakeModel, HoldingEchoModel

pytestmark = requires_postgres

ONE_TASK = "\n[work]\nmax_running_tasks_per_worker = 1\n"


@pytest.fixture
def fake_model() -> FakeModel:
    return HoldingEchoModel()


def start_conversation(base_url: str, text: str) -> str:
    """A new conversation's first turn, run to its end: the stream's last line."""
    with httpx.Client(base_url=base_url, timeout=30.0) as client:
        started = client.post("/api/turns", json={"agent_id": "pydantic_ai", "text": text})
    assert started.status_code == 200, started.text
    return [line for line in started.text.splitlines() if line.startswith("data: ")][-1]


def test_a_turn_waits_queued_while_the_worker_is_full(
    local_gpt: FakeLocalGPTServer, fake_model: HoldingEchoModel, tools_url: str, tmp_path: Path
) -> None:
    config = tmp_path / "robinauts.toml"
    config.write_text(stack.config_for(local_gpt.base_url, tools_url) + ONE_TASK)
    with (
        stack.database(config) as url,
        stack.server(config, url, env=stack.API_KEY) as server,
        httpx.Client(base_url=server, timeout=30.0) as api,
        ThreadPoolExecutor(2) as threads,
    ):
        held = threads.submit(start_conversation, server, "hold")
        try:
            assert fake_model.asked.wait(10)
            waiting = threads.submit(start_conversation, server, "hello")
            while len(items := api.get("/api/conversations").json()["items"]) < 2:
                time.sleep(0.05)
            second = next(c["id"] for c in items if c["title"] == "hello")

            # A worker with room would have claimed it at once.
            time.sleep(1)
            opened = api.get(f"/api/conversations/{second}").json()
            assert opened["run_id"] is not None
            assert [m["role"] for m in opened["messages"]] == ["user"]
            assert len(local_gpt.received) == 1
        finally:
            fake_model.release.set()
        assert '"RUN_FINISHED"' in held.result(timeout=30)
        assert '"RUN_FINISHED"' in waiting.result(timeout=30)
        assert len(local_gpt.received) == 2
