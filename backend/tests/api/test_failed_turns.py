# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""What the API says of a failed turn, and which answers it lets be retried."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from util.controller_db import requires_postgres
from util.fake_openai import FakeModel, PoisonEchoModel
from util.stack import AGENTS

pytestmark = requires_postgres


@pytest.fixture
def fake_model() -> FakeModel:
    return PoisonEchoModel()


def events(body: str) -> list[dict[str, Any]]:
    return [
        json.loads(line.removeprefix("data: "))
        for line in body.splitlines()
        if line.startswith("data: ")
    ]


def start(api: httpx.Client, agent: str, text: str) -> tuple[httpx.Response, str]:
    """A new conversation's first turn, run to its end, and the conversation's id."""
    started = api.post("/robinauts/api/turns", json={"agent_id": agent, "text": text})
    assert started.status_code == 200, started.text
    return started, started.headers["x-robinauts-conversation-id"]


@pytest.mark.parametrize("agent", AGENTS)
def test_a_failed_turn_says_how_it_ended_and_not_why(api: httpx.Client, agent: str) -> None:
    started, cid = start(api, agent, "poison")
    assert events(started.text)[-1]["type"] == "RUN_ERROR"

    opened = api.get(f"/robinauts/api/conversations/{cid}").json()
    ended = opened["ended_badly"]
    assert (ended["run_id"], ended["state"]) == (
        started.headers["x-robinauts-run-id"],
        "failed",
    )
    assert set(ended) == {"run_id", "state", "ended_at"}
    # What the model server said went wrong is the operator's to read, not the user's.
    assert "poisoned" not in started.text
    assert "poisoned" not in json.dumps(opened)


def test_only_a_failed_answer_can_be_retried(api: httpx.Client) -> None:
    started, cid = start(api, "pydantic_ai", "hello")
    assert events(started.text)[-1]["type"] == "RUN_FINISHED"
    answer = api.get(f"/robinauts/api/conversations/{cid}").json()["messages"][1]
    assert answer["failed"] is False

    refused = api.post(f"/robinauts/api/conversations/{cid}/turns", json={"retry": answer["id"]})
    assert refused.status_code == 404
