# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Tasks through the API (``docs/specs/wire.md``, "Without streaming"): what no screen shows,
since no screen starts one."""

from __future__ import annotations

import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx
import pytest

from util.controller_db import requires_postgres
from util.fake_openai import FakeModel, HoldingEchoModel

pytestmark = requires_postgres

AGENT = "pydantic_ai"


class HoldingPoisonModel(HoldingEchoModel):
    """Holds ``hold`` until released, and fails a question starting with ``poison``."""

    def reply(self, messages: list[dict[str, Any]]) -> str:
        text = super().reply(messages)
        if text.startswith("poison"):
            raise RuntimeError("the message is poisoned")
        return text


@pytest.fixture
def fake_model() -> FakeModel:
    return HoldingPoisonModel()


def start(api: httpx.Client, key: str, **body: Any) -> httpx.Response:
    return api.post(
        "/api/tasks", json={"agent_id": AGENT, "prompt": "hello", "idempotency_key": key, **body}
    )


def test_a_task_answers_once_its_turn_has_ended_and_a_repeat_finds_it(api: httpx.Client) -> None:
    key = str(uuid.uuid4())
    labels = {"neorc_run_id": "run-1", "neorc_task_id": "task-1"}
    started = start(api, key, labels=labels)
    assert started.status_code == 201, started.text
    task = started.json()
    assert set(task) == {"task_id", "conversation_id", "run_id", "state"}
    assert task["task_id"] == task["conversation_id"]

    done = api.get(f"/api/tasks/{task['task_id']}", params={"wait": 20}).json()
    default = next(a["model"] for a in api.get("/api/agents").json()["items"] if a["id"] == AGENT)
    assert (done["state"], done["answer"]) == ("finished", "hello")
    assert (done["agent"], done["model"], done["labels"]) == (AGENT, default, labels)
    assert done["run_id"] == task["run_id"]
    assert done["ended_at"] is not None

    again = start(api, key, labels=dict(reversed(labels.items())))
    assert again.status_code == 200, again.text
    assert again.json() == {**task, "state": "finished"}
    for changed in ({"prompt": "else"}, {"model_id": default}, {"labels": {}}):
        refused = start(api, key, **{"labels": labels, **changed})
        assert refused.status_code == 409, changed


def test_a_tasks_conversation_takes_no_other_turn_of_any_shape(api: httpx.Client) -> None:
    started = start(api, str(uuid.uuid4()), prompt="poison")
    task = started.json()["task_id"]
    failed = api.get(f"/api/tasks/{task}", params={"wait": 20}).json()
    assert (failed["state"], failed["answer"]) == ("failed", None)
    # Why it failed is the operator's to read.
    assert "poisoned" not in str(failed)

    question, answer = (m["id"] for m in api.get(f"/api/conversations/{task}").json()["messages"])
    for body in (
        {"parent_id": answer, "text": "more"},
        {"edit": question, "text": "again"},
        {"regenerate": answer},
        {"retry": answer},
        {"resume": answer},
    ):
        refused = api.post(f"/api/conversations/{task}/turns", json=body)
        assert refused.status_code == 409, body


def test_a_wait_holds_until_the_turn_ends_and_a_cancel_ends_it(
    api: httpx.Client, fake_model: HoldingEchoModel
) -> None:
    task = start(api, str(uuid.uuid4()), prompt="hold").json()["task_id"]
    assert fake_model.asked.wait(10)
    began = time.monotonic()
    held = api.get(f"/api/tasks/{task}", params={"wait": 1}).json()
    assert time.monotonic() - began >= 0.9
    assert (held["state"], held["answer"], held["ended_at"]) == ("active", None, None)

    with ThreadPoolExecutor(1) as pool, httpx.Client(base_url=api.base_url, timeout=40) as other:
        waiting = pool.submit(other.get, f"/api/tasks/{task}", params={"wait": 30})
        time.sleep(0.5)
        began = time.monotonic()
        assert api.post(f"/api/tasks/{task}/cancel").status_code == 204
        ended = waiting.result(timeout=20).json()
    assert time.monotonic() - began < 15, "the wait was not woken by the turn's end"
    fake_model.release.set()
    assert (ended["state"], ended["answer"]) == ("cancelled", None)
    # An ended task is left as it ended.
    assert api.post(f"/api/tasks/{task}/cancel").status_code == 204
    assert api.get(f"/api/tasks/{task}").json()["state"] == "cancelled"


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ({"agent_id": "nobody"}, 404),
        ({"model_id": "nothing"}, 422),
        ({"prompt": "  "}, 422),
        ({"text": "a field the body does not know"}, 422),
        ({"idempotency_key": "k" * 201}, 422),
        ({"idempotency_key": "has a space"}, 422),
        ({"idempotency_key": ""}, 422),
        ({"labels": {f"l{n}": "v" for n in range(17)}}, 422),
        ({"labels": {"not a name": "v"}}, 422),
        ({"labels": {"name": "v" * 257}}, 422),
    ],
)
def test_a_task_the_api_cannot_start_is_refused(
    api: httpx.Client, body: dict[str, Any], status: int
) -> None:
    refused = start(api, str(uuid.uuid4()), **body)
    assert refused.status_code == status, refused.text


def test_what_is_not_a_task_answers_404(api: httpx.Client) -> None:
    conversation = api.post("/api/turns", json={"agent_id": AGENT, "text": "hello"})
    cid = conversation.headers["x-robinauts-conversation-id"]
    for missing in (cid, str(uuid.uuid4())):
        assert api.get(f"/api/tasks/{missing}").status_code == 404
        assert api.post(f"/api/tasks/{missing}/cancel").status_code == 404
    task = start(api, str(uuid.uuid4())).json()["task_id"]
    assert api.get(f"/api/tasks/{task}", params={"wait": 31}).status_code == 422
