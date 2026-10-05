# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""A new conversation with one message and its answer."""

from __future__ import annotations

import httpx

from util.controller_db import requires_postgres

pytestmark = requires_postgres


def test_a_new_conversation_answers_its_first_message(api: httpx.Client) -> None:
    started = api.post("/api/turns", json={"agent_id": "pydantic_ai", "text": "hello"})
    assert started.status_code == 200, started.text
    last = [line for line in started.text.splitlines() if line.startswith("data: ")][-1]
    assert '"RUN_FINISHED"' in last

    cid = started.headers["x-robinauts-conversation-id"]
    messages = api.get(f"/api/conversations/{cid}").json()["messages"]
    assert [(m["role"], m["parts"], m["failed"]) for m in messages] == [
        ("user", [{"kind": "text", "text": "hello"}], False),
        ("assistant", [{"kind": "text", "text": "hello"}], False),
    ]
