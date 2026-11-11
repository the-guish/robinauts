# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Archiving through the API: off the listing, still there, and back with its next turn."""

from __future__ import annotations

import uuid

import httpx
import pytest

from util.controller_db import requires_postgres
from util.fake_openai import FakeModel, HoldingEchoModel

pytestmark = requires_postgres


@pytest.fixture
def fake_model() -> FakeModel:
    return HoldingEchoModel()


def listed(api: httpx.Client) -> list[str]:
    return [c["id"] for c in api.get("/api/conversations").json()["items"]]


def archive(api: httpx.Client, cid: str) -> int:
    return api.post(f"/api/conversations/{cid}/archive").status_code


def test_archive_hides_keeps_and_comes_back(
    api: httpx.Client, fake_model: HoldingEchoModel
) -> None:
    started = api.post("/api/turns", json={"agent_id": "pydantic_ai", "text": "hello"})
    assert started.status_code == 200, started.text
    cid = started.headers["x-robinauts-conversation-id"]
    assert cid in listed(api)

    # Archived: off the listing, and still opened by its id. A second archive changes nothing.
    assert archive(api, cid) == 204
    assert cid not in listed(api)
    assert archive(api, cid) == 204
    answer = api.get(f"/api/conversations/{cid}").json()["messages"][1]["id"]

    # Its next turn brings it back, and archiving it while that turn runs leaves the turn
    # running and the conversation archived once the turn has ended.
    turns = f"/api/conversations/{cid}/turns"
    with api.stream("POST", turns, json={"parent_id": answer, "text": "hold"}) as held:
        assert fake_model.asked.wait(10)
        assert cid in listed(api)
        assert archive(api, cid) == 204
        fake_model.release.set()
        assert '"RUN_FINISHED"' in held.read().decode()
    assert cid not in listed(api)
    opened = api.get(f"/api/conversations/{cid}").json()
    assert opened["messages"][-1]["parts"][0]["text"] == "hold"

    assert archive(api, str(uuid.uuid4())) == 404
