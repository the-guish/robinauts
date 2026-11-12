# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""An agent's models: listed with it, its default first, and a turn or a change of model
outside them refused. The interface offers only those models, so only a client of its own
sends another."""

from __future__ import annotations

import httpx

from util.controller_db import requires_postgres

pytestmark = requires_postgres


def test_an_agent_runs_only_on_the_models_it_lists(api: httpx.Client) -> None:
    agents = {a["id"]: a for a in api.get("/api/agents").json()["items"]}
    assert (agents["pinned"]["model"], agents["pinned"]["models"]) == (
        "local_gpt_2",
        ["local_gpt_2"],
    )
    assert agents["pydantic_ai"]["models"] == ["local_gpt", "local_gpt_2"]

    refused = api.post(
        "/api/turns",
        json={"agent_id": "pinned", "model_id": "local_gpt", "text": "pinned refused"},
    )
    assert refused.status_code == 422
    assert refused.json() == {
        "error": "UnknownModelError",
        "detail": "local_gpt is not one of agent pinned's models",
    }
    listed = api.get("/api/conversations", params={"limit": 200}).json()["items"]
    assert "pinned refused" not in [c["title"] for c in listed]

    started = api.post("/api/turns", json={"agent_id": "pinned", "text": "pinned one"})
    assert started.status_code == 200, started.text
    cid = started.headers["x-robinauts-conversation-id"]
    assert api.get(f"/api/conversations/{cid}").json()["conversation"]["model"] == "local_gpt_2"

    moved = api.put(f"/api/conversations/{cid}/model", json={"model_id": "local_gpt"})
    assert (moved.status_code, moved.json()["error"]) == (422, "UnknownModelError")
    answer = api.get(f"/api/conversations/{cid}").json()["messages"][-1]["id"]
    sent = api.post(
        f"/api/conversations/{cid}/turns",
        json={"parent_id": answer, "text": "pinned two", "model_id": "local_gpt"},
    )
    assert (sent.status_code, sent.json()["error"]) == (422, "UnknownModelError")
