# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Tasks over a controller on the echo engine and the in-process store: what
tests/api/test_tasks.py holds on PostgreSQL, again in process, and a task's owner."""

from __future__ import annotations

import uuid

import pytest
from echo_controller import start_echo_controller
from test_web_turns import client

from robinauts.controller.contract.domain import Identity
from robinauts.shared_domain.domain import SessionNotFoundError
from util.aio import asyncio_test


@asyncio_test
async def test_a_task_runs_once_answers_once_and_takes_no_other_turn() -> None:
    async with client() as http:
        body = {"agent_id": "echo", "prompt": "hello", "idempotency_key": "k", "labels": {"a": "b"}}
        started = await http.post("/api/tasks", json=body)
        assert started.status_code == 201, started.text
        task = started.json()["task_id"]
        done = (await http.get(f"/api/tasks/{task}", params={"wait": 5})).json()
        assert (done["state"], done["labels"]) == ("finished", {"a": "b"})
        assert done["answer"] == "The tool said: hello"
        assert (await http.post("/api/tasks", json=body)).status_code == 200
        other = await http.post("/api/tasks", json={**body, "prompt": "else"})
        assert other.status_code == 409

        answer = (await http.get(f"/api/conversations/{task}")).json()["messages"][1]["id"]
        turn = await http.post(f"/api/conversations/{task}/turns", json={"regenerate": answer})
        assert turn.status_code == 409
        assert (await http.post(f"/api/tasks/{task}/cancel")).status_code == 204

        assert (
            await http.post("/api/tasks", json={**body, "idempotency_key": "a", "agent_id": "x"})
        ).status_code == 404
        unknown_model = await http.post(
            "/api/tasks", json={**body, "idempotency_key": "m", "model_id": "x"}
        )
        assert unknown_model.status_code == 422
        assert (await http.get(f"/api/tasks/{uuid.uuid4()}")).status_code == 404


@asyncio_test
async def test_a_task_is_its_owners_alone() -> None:
    lifecycle = await start_echo_controller()
    controller = lifecycle.composed.controller
    me = await controller.ensure_user(Identity("local", "me"))
    you = await controller.ensure_user(Identity("local", "you"))
    opened, _ = await controller.start_one_shot(
        me, agent="echo", model=None, prompt="hi", key="k", labels={}
    )
    with pytest.raises(SessionNotFoundError):
        await controller.open_one_shot(you, opened.session.id)
    with pytest.raises(SessionNotFoundError):
        await controller.cancel_one_shot(you, opened.session.id)
    # The same key is another task for another owner.
    yours, created = await controller.start_one_shot(
        you, agent="echo", model=None, prompt="hi", key="k", labels={}
    )
    assert created
    assert yours.session.id != opened.session.id
    await lifecycle.stop()
