# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The sessions tool: what it searches, what it leaves out, and what it says back."""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from robinauts.agent_engines.contract.domain import FunctionTool, FunctionToolError
from robinauts.shared_domain.domain import (
    Message,
    MessagePart,
    ReasoningPart,
    Role,
    Session,
    SessionNotFoundError,
    TextPart,
    ToolCallPart,
    ToolResultPart,
)
from robinauts.tools import sessions
from robinauts.tools.contract.ports import ScopedData, TurnScope
from robinauts.tools.sessions import search
from util.aio import asyncio_test

START = datetime(2026, 1, 1, tzinfo=UTC)
OWNER = uuid.uuid4()
PROJECT = uuid.uuid4()


class Data(ScopedData):
    """Sessions newest first, as the controller's bound reads give them."""

    def __init__(self) -> None:
        self.sessions_by_id: dict[uuid.UUID, Session] = {}
        self.messages_of: dict[uuid.UUID, list[Message]] = {}
        self.pages = 0

    def add(
        self, title: str, *texts: str | Sequence[MessagePart], role: Role = Role.USER
    ) -> uuid.UUID:
        at = START + timedelta(hours=len(self.sessions_by_id))
        session = Session(uuid.uuid4(), OWNER, "keeper", "echo", at, at, title, project_id=PROJECT)
        self.sessions_by_id[session.id] = session
        self.messages_of[session.id] = [
            Message(
                uuid.uuid4(),
                session.id,
                None,
                role,
                (TextPart(text),) if isinstance(text, str) else tuple(text),
                at + timedelta(minutes=n),
            )
            for n, text in enumerate(texts)
        ]
        return session.id

    async def sessions(self, limit: int, after: Session | None = None) -> list[Session]:
        self.pages += 1
        ordered = sorted(self.sessions_by_id.values(), key=lambda s: s.updated_at, reverse=True)
        if after is not None:
            ordered = ordered[ordered.index(after) + 1 :]
        return ordered[:limit]

    async def messages(self, session_id: uuid.UUID) -> list[Message]:
        if session_id not in self.messages_of:
            raise SessionNotFoundError(str(session_id))
        return self.messages_of[session_id]


def tool(data: Data, current: uuid.UUID | None = None) -> FunctionTool:
    [found] = sessions.load().tools_for(TurnScope(current or uuid.uuid4(), "keeper", data))
    return found


async def call(data: Data, arguments: Mapping[str, Any], current: uuid.UUID | None = None) -> Any:
    return json.loads(await tool(data, current).call(arguments))


def test_load_gives_the_tool_its_id_and_one_function() -> None:
    provider = sessions.load()
    assert provider.id == "sessions"
    found = tool(Data())
    assert found.name == "search_sessions"
    assert found.input_schema["required"] == ["query"]


@pytest.mark.parametrize(
    ("arguments", "problem"),
    [
        ({}, "`query` must be a string"),
        ({"query": "  "}, "`query` must be a string"),
        ({"query": 7}, "`query` must be a string"),
        ({"query": "x" * 201}, "at most 200 characters"),
        ({"query": "x", "limit": 0}, "`limit` must be"),
        ({"query": "x", "limit": 11}, "`limit` must be"),
        ({"query": "x", "limit": True}, "`limit` must be"),
        ({"query": "x", "limit": "3"}, "`limit` must be"),
        ({"query": "x", "owner": "someone"}, "Unknown argument(s): owner"),
    ],
)
@asyncio_test
async def test_wrong_arguments_are_refused_in_words_the_model_can_act_on(
    arguments: Mapping[str, Any], problem: str
) -> None:
    with pytest.raises(FunctionToolError, match=problem.replace("(", r"\(").replace(")", r"\)")):
        await tool(Data()).call(arguments)


@asyncio_test
async def test_a_message_matches_when_it_holds_every_word_case_aside() -> None:
    data = Data()
    both = data.add("Trip", "We planned the ZEBRA trip in May")
    data.add("Other", "zebra alone")
    found = await call(data, {"query": "zebra may"})
    assert [c["session_id"] for c in found["sessions"]] == [str(both)]
    [match] = found["sessions"][0]["matches"]
    assert match == {
        "role": "user",
        "created_at": START.isoformat(),
        "excerpt": "We planned the ZEBRA trip in May",
    }


@asyncio_test
async def test_only_what_the_person_and_the_agent_wrote_is_searched() -> None:
    data = Data()
    data.add(
        "Tools",
        [ReasoningPart("zebra in thought")],
        [ToolCallPart("c", "find", {"q": "zebra"})],
        [ToolResultPart("c", "zebra from a tool")],
    )
    data.add("Tool role", "zebra said by a tool", role=Role.TOOL)
    assert (await call(data, {"query": "zebra"}))["sessions"] == []


@asyncio_test
async def test_the_session_of_the_turn_is_left_out() -> None:
    data = Data()
    current = data.add("Now", "zebra")
    earlier = data.add("Before", "zebra")
    found = await call(data, {"query": "zebra"}, current=current)
    assert [c["session_id"] for c in found["sessions"]] == [str(earlier)]


@asyncio_test
async def test_the_newest_sessions_come_first_up_to_the_limit() -> None:
    data = Data()
    ids = [
        data.add(f"Talk {n}", "zebra", "more zebra", "zebra again", "zebra once more")
        for n in range(4)
    ]
    found = await call(data, {"query": "zebra", "limit": 2})
    assert [c["session_id"] for c in found["sessions"]] == [str(ids[3]), str(ids[2])]
    assert all(len(c["matches"]) == search.MATCHES_PER_SESSION for c in found["sessions"])
    assert found["sessions"][0]["title"] == "Talk 3"


@asyncio_test
async def test_a_title_that_matches_finds_its_session() -> None:
    data = Data()
    titled = data.add("Zebra planning", "nothing here")
    found = await call(data, {"query": "zebra"})
    assert [(c["session_id"], c["matches"]) for c in found["sessions"]] == [(str(titled), [])]


@asyncio_test
async def test_a_long_message_is_quoted_around_the_first_word_found() -> None:
    data = Data()
    data.add("Long", "a" * 1000 + " zebra " + "b" * 1000)
    [session] = (await call(data, {"query": "zebra"}))["sessions"]
    excerpt = session["matches"][0]["excerpt"]
    assert excerpt.startswith("…")
    assert excerpt.endswith("…")
    assert "zebra" in excerpt
    assert len(excerpt) <= search.EXCERPT + 2


@asyncio_test
async def test_one_call_reads_at_most_the_newest_max_sessions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(search, "MAX_SESSIONS", 5)
    monkeypatch.setattr(search, "PAGE", 2)
    data = Data()
    oldest = data.add("Oldest", "zebra")
    for n in range(9):
        data.add(f"Talk {n}", "nothing")
    found = await call(data, {"query": "zebra"})
    assert found["sessions"] == []
    assert found["sessions_read"] == 5
    assert str(oldest) not in json.dumps(found)
    assert data.pages == 3
