# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""A task payload through the API (``docs/specs/wire.md``, "Task payloads"): structured data
beside a question's text, stored on the question and handed to the engine at every turn that
answers it.

The server is its own, on the echo engine, which says a turn's task payload back after its
answer: what shows the payload reached the engine, through the store and the worker."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from robinauts.agent_engines.echo_engine.engine import TASK_PAYLOAD
from util import stack
from util.controller_db import requires_postgres

pytestmark = requires_postgres

CONFIG = """
[model_providers.echo]
kind = "anthropic"
api_key_env = "ECHO_API_KEY"

[models.echo]
provider = "echo"
name = "echo"

[agents.echo]
title = "Echo"
system_prompt = ""
model = "echo"
engine = "echo"
"""

PAYLOAD = {"task_key": "code_task/1", "repositories": {"app": {"branch": "develop"}}}
OTHER = {"task_key": "code_task/1", "repositories": {"app": {"branch": "main"}}}


@pytest.fixture(scope="module")
def api(tmp_path_factory: pytest.TempPathFactory) -> Iterator[httpx.Client]:
    config = tmp_path_factory.mktemp("payloads") / "robinauts.toml"
    config.write_text(CONFIG)
    env = {"ECHO_API_KEY": "not-a-real-key"}
    with stack.database(config) as url, stack.server(config, url, env=env) as server:
        with httpx.Client(base_url=server, timeout=30.0, headers={"origin": server}) as client:
            yield client


def said(payload: dict[str, Any]) -> str:
    """What the echo engine says after its answer for a turn with ``payload``."""
    return TASK_PAYLOAD + json.dumps(payload, sort_keys=True)


def messages(api: httpx.Client, cid: str) -> list[dict[str, Any]]:
    return api.get(f"/api/conversations/{cid}").json()["messages"]


def text(message: dict[str, Any]) -> str:
    return "".join(part["text"] for part in message["parts"] if part["kind"] == "text")


def ok(response: httpx.Response) -> httpx.Response:
    assert response.status_code == 200, response.text
    return response


def test_a_payload_is_kept_on_its_question_and_reaches_every_turn_that_answers_it(
    api: httpx.Client,
) -> None:
    started = ok(
        api.post("/api/turns", json={"agent_id": "echo", "text": "fix", "task_payload": PAYLOAD})
    )
    cid = started.headers["x-robinauts-conversation-id"]
    turns = f"/api/conversations/{cid}/turns"
    question, answer = messages(api, cid)
    assert question["task_payload"] == PAYLOAD
    assert answer["task_payload"] is None
    assert text(answer).endswith(said(PAYLOAD))

    # A follow-up with text alone has none, and its turn hands the engine none.
    ok(api.post(turns, json={"parent_id": answer["id"], "text": "and then?"}))
    follow_up, follow_answer = messages(api, cid)[2:]
    assert follow_up["task_payload"] is None
    assert TASK_PAYLOAD not in text(follow_answer)

    # A regeneration answers the question as it was asked: with its payload.
    ok(api.post(turns, json={"regenerate": answer["id"]}))
    regenerated = messages(api, cid)[1]
    assert regenerated["id"] != answer["id"]
    assert text(regenerated).endswith(said(PAYLOAD))
    refused = api.post(turns, json={"regenerate": regenerated["id"], "task_payload": OTHER})
    assert refused.status_code == 422, refused.text
    assert "goes with a new question or an edit" in refused.json()["detail"]

    # An edit keeps the edited question's payload, unless it gives another.
    ok(api.post(turns, json={"edit": question["id"], "text": "fix, edited"}))
    kept = messages(api, cid)
    assert (kept[0]["task_payload"], text(kept[1]).endswith(said(PAYLOAD))) == (PAYLOAD, True)
    ok(api.post(turns, json={"edit": kept[0]["id"], "text": "fix", "task_payload": OTHER}))
    replaced = messages(api, cid)
    assert replaced[0]["task_payload"] == OTHER
    assert text(replaced[1]).endswith(said(OTHER))


@pytest.mark.parametrize(
    ("payload", "detail"),
    [
        ({"repositories": {}}, "names its kind in task_key"),
        ({"task_key": ""}, "names its kind in task_key"),
        ({"task_key": "x", "blob": "x" * 70_000}, "is over 65536 bytes"),
    ],
)
def test_a_payload_that_is_not_a_task_is_refused_before_anything_is_stored(
    api: httpx.Client, payload: dict[str, Any], detail: str
) -> None:
    before = api.get("/api/conversations").json()["items"]
    refused = api.post(
        "/api/turns", json={"agent_id": "echo", "text": "fix", "task_payload": payload}
    )
    assert refused.status_code == 422, refused.text
    assert detail in refused.json()["detail"]
    assert api.get("/api/conversations").json()["items"] == before
