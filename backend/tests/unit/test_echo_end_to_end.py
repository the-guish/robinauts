# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The web app over the echo engine and the in-memory store, driven the way the browser does."""

from __future__ import annotations

from test_web_turns import client, events

from aio import asyncio_test


@asyncio_test
async def test_a_conversation_from_the_first_message_to_its_deletion() -> None:
    async with client() as http:
        session = (await http.get("/auth/session")).json()
        assert session["sign_in"] is False
        assert session["user"]["provider"] == "local"
        agents = (await http.get("/api/agents")).json()["items"]
        assert [a["id"] for a in agents] == ["echo"]
        models = (await http.get("/api/models")).json()["items"]
        assert [m["id"] for m in models] == ["echo"]

        started = await http.post("/api/turns", json={"agent_id": "echo", "text": "hello"})
        assert events(started.text)[-1]["type"] == "RUN_FINISHED"
        cid = started.headers["x-robinauts-conversation-id"]

        listed = (await http.get("/api/conversations")).json()["items"]
        assert [c["id"] for c in listed] == [cid]

        opened = (await http.get(f"/api/conversations/{cid}")).json()
        question, answer = opened["messages"]
        assert question["parts"] == [{"kind": "text", "text": "hello"}]
        call, result, text = answer["parts"]
        assert (call["kind"], call["name"], call["arguments"]) == (
            "tool_call",
            "echo",
            {"text": "hello"},
        )
        assert result == {
            "kind": "tool_result",
            "call_id": call["call_id"],
            "text": "hello",
            "is_error": False,
        }
        assert text == {"kind": "text", "text": "The tool said: hello"}

        again = await http.post(
            f"/api/conversations/{cid}/turns", json={"text": "again", "parent_id": answer["id"]}
        )
        assert events(again.text)[-1]["type"] == "RUN_FINISHED"
        rid = again.headers["x-robinauts-run-id"]
        replayed = events(
            (await http.get(f"/api/conversations/{cid}/runs/{rid}/events?after=0")).text
        )
        assert replayed[0]["type"] == "RUN_STARTED"
        assert replayed[-1]["type"] == "RUN_FINISHED"
        said = "".join(e["delta"] for e in replayed if e["type"] == "TEXT_MESSAGE_CONTENT")
        assert said == "The tool said: again"
        opened = (await http.get(f"/api/conversations/{cid}")).json()
        assert [m["role"] for m in opened["messages"]] == ["user", "assistant"] * 2

        renamed = await http.patch(f"/api/conversations/{cid}", json={"title": "Echoes"})
        assert renamed.json()["title"] == "Echoes"
        assert (await http.delete(f"/api/conversations/{cid}")).status_code == 204
        assert (await http.get("/api/conversations")).json()["items"] == []
