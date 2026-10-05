# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""A conversation through the API: messages, a cancel in flight, an edit, a regeneration."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from util.controller_db import requires_postgres
from util.fake_openai import FakeModel, HoldingEchoModel

pytestmark = requires_postgres


@pytest.fixture
def fake_model() -> FakeModel:
    return HoldingEchoModel()


def last_event(body: str) -> dict[str, Any]:
    data = [line for line in body.splitlines() if line.startswith("data: ")]
    return json.loads(data[-1].removeprefix("data: "))


def assert_finished(response: httpx.Response) -> None:
    assert response.status_code == 200, response.text
    ended = last_event(response.text)
    assert (ended["type"], ended.get("outcome")) == ("RUN_FINISHED", None), ended


def thread(api: httpx.Client, cid: str) -> list[tuple[str, str]]:
    messages = api.get(f"/api/conversations/{cid}").json()["messages"]
    return [(m["role"], "".join(p["text"] for p in m["parts"])) for m in messages]


def test_a_conversation_with_a_cancel_an_edit_and_a_regeneration(
    api: httpx.Client, fake_model: HoldingEchoModel
) -> None:
    started = api.post("/api/turns", json={"agent_id": "pydantic_ai", "text": "hello"})
    assert_finished(started)
    cid = started.headers["x-robinauts-conversation-id"]
    turns = f"/api/conversations/{cid}/turns"
    assert thread(api, cid) == [("user", "hello"), ("assistant", "hello")]

    first_answer = api.get(f"/api/conversations/{cid}").json()["messages"][1]["id"]
    assert_finished(api.post(turns, json={"parent_id": first_answer, "text": "second"}))
    messages = api.get(f"/api/conversations/{cid}").json()["messages"]
    second_question, second_answer = messages[2]["id"], messages[3]["id"]
    assert thread(api, cid)[2:] == [("user", "second"), ("assistant", "second")]

    with api.stream("POST", turns, json={"parent_id": second_answer, "text": "hold"}) as held:
        assert held.status_code == 200
        assert fake_model.asked.wait(10)
        run = held.headers["x-robinauts-run-id"]
        cancelled = api.post(f"/api/conversations/{cid}/runs/{run}/cancel")
        assert cancelled.status_code == 204, cancelled.text
        ended = last_event(held.read().decode())
    fake_model.release.set()
    assert ended["type"] == "RUN_FINISHED", ended
    assert ended["outcome"] is not None, ended
    opened = api.get(f"/api/conversations/{cid}").json()
    assert (opened["ended_badly"]["run_id"], opened["ended_badly"]["state"]) == (run, "cancelled")

    assert_finished(api.post(turns, json={"edit": second_question, "text": "second, edited"}))
    edited = api.get(f"/api/conversations/{cid}").json()["messages"]
    assert thread(api, cid) == [
        ("user", "hello"),
        ("assistant", "hello"),
        ("user", "second, edited"),
        ("assistant", "second, edited"),
    ]

    assert_finished(api.post(turns, json={"regenerate": edited[3]["id"]}))
    regenerated = api.get(f"/api/conversations/{cid}").json()["messages"]
    assert [m["id"] for m in regenerated[:3]] == [m["id"] for m in edited[:3]]
    assert regenerated[3]["id"] != edited[3]["id"]
    assert thread(api, cid)[3] == ("assistant", "second, edited")
