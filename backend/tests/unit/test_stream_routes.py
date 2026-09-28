# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The streaming routes, over the real application, the real executor and the fakes.

What is tested here is what the routes add to the run lifecycle
``test_turn_lifecycle.py`` and ``test_run_watch.py`` have already proved: the
refusals that must land **before** a stream begins, the headers a client
re-attaches with, the events that go out and the ways a stream ends.

The services are the real ones over the in-memory store, with the real executor
and the real signals under them, so a turn really runs in the background while
the response is being written. Nothing sleeps: the engine waits at gates the
test opens.

**Two clients, because a stream has two halves.** ``httpx``'s ASGI transport
runs the application to its end before it answers, which is exactly right for a
turn the test lets finish and for every refusal; ``tests/sse.py`` drives the
application as a server does, which is what a test of a heartbeat, of a quiet
run or of a client that goes away in the middle needs.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
from ag_ui.core import EventType

from aio import asyncio_test
from contracts.tool_servers import SEARCH
from conversations import AGENT, MODEL, OTHER_MODEL, agent_definition, conversation
from fakes import Gate, MemoryConversationStore, MemoryToolServers, Step, calls, says
from robinauts.api import (
    CONVERSATION_ID_HEADER,
    GENERIC_DETAIL,
    GONE_CODE,
    GONE_DETAIL,
    INTERNAL_CODE,
    INTERNAL_ERROR,
    MAX_POSITION_DIGITS,
    NO_LONGER_OFFERED_DETAIL,
    NOT_FOUND_DETAIL,
    NOT_FOUND_ERROR,
    NOT_OFFERED_DETAIL,
    NOT_WIRED,
    ONE_FORM,
    POSITION_AHEAD,
    POSITIONS_DISAGREE,
    QUIET_CODE,
    QUIET_RUN_DETAIL,
    RUN_ID_HEADER,
    SSE_MEDIA_TYPE,
    UNKNOWN_FIELD,
    UNREADABLE_POSITION,
    UNREADABLE_RULES,
    NewChatRequest,
    TurnRequest,
    create_api,
    openapi_document,
    session_cookie,
)
from robinauts.application import StartedTurn
from robinauts.domain import (
    MAX_CONFIG_ID_CHARS,
    MAX_MESSAGE_CHARS,
    AnswerCompleted,
    AnswerReasoningDelta,
    AnswerStarted,
    AnswerTextDelta,
    ReasoningPart,
    RunState,
    ToolResult,
    ToolServerConfig,
    User,
    text_parts,
)
from sse import Sent, Streamed, events, streaming
from turns import NOW, SOMEBODY_ELSE, Wiring, settled, stored_messages
from turns import wired as wired_services
from turns import written as stream_reached
from webapp import JSON, PUBLIC_URL, client_on, open_session, serving, wired

ANSWER = "Someone who plays fair."

QUESTION = "What is a robinaut?"

NOWHERE = uuid.UUID("99999999-9999-4999-8999-999999999999")
"""An id nothing here has: what "not there" is asked with."""

WRITE = {**JSON, "origin": PUBLIC_URL}
"""What a browser of this deployment sends with a write, and no more."""

CROSS_SITE = {**WRITE, "sec-fetch-site": "cross-site"}
"""The same write, from a page that is not ours."""

HOST = PUBLIC_URL.removeprefix("https://")

BEAT = 0.01
"""A heartbeat a test does not have to wait fifteen seconds for."""


@dataclass(frozen=True, slots=True)
class Served:
    """The application, the services under it, a client, and the person asking."""

    app: object
    client: httpx.AsyncClient
    wiring: Wiring
    user: User
    secret: str

    def sending(self, **more: str) -> dict[str, str]:
        """The headers a request of that person's carries, driven as a server."""
        return {
            "host": HOST,
            "cookie": f"{session_cookie(secure=True)}={self.secret}",
            **more,
        }

    async def begun(self, text: str = QUESTION) -> StartedTurn:
        """A conversation of theirs with one question and a run in flight."""
        return await self.wiring.turns.begin(self.user, agent_id=AGENT, text=text)


@asynccontextmanager
async def served(
    *steps: Step,
    store: MemoryConversationStore | None = None,
    quiet_seconds: float = 300.0,
    wait_seconds: float = 30.0,
    **changes: Any,
) -> AsyncIterator[Served]:
    """The real application with a session open, over services on the fakes.

    ``changes`` is what else ``turns.wired`` takes: the agent, the tool
    servers and the port over them, for a turn that calls a tool.
    """
    deployment = wired()
    secret, user = await open_session(deployment)
    services = wired_services(
        *steps, store=store, quiet_seconds=quiet_seconds, wait_seconds=wait_seconds, **changes
    )
    app = create_api(
        deployment.sign_in,
        conversations=services.conversations,
        turns=services.turns,
        watch=services.watch,
    )
    async with client_on(app) as client:
        client.cookies.set(session_cookie(secure=True), secret)
        yield Served(app=app, client=client, wiring=services, user=user, secret=secret)


class VanishingRun(MemoryConversationStore):
    """A store whose conversation goes between the two ownership checks.

    The route asks whether the run is this person's before it opens a stream,
    and the watcher asks again for itself (``application.Watch``). A deletion
    landing in the gap is the one way the second can refuse where the first did
    not, and it is not a fault: the run is gone, which is a thing to say.
    """

    def __init__(self) -> None:
        super().__init__()
        self.deletes = False
        self.reads = 0

    async def run_by_id(self, run_id: uuid.UUID) -> object:
        run = await super().run_by_id(run_id)
        if not self.deletes or run is None:
            return run
        self.reads += 1
        if self.reads < 2:
            return run
        self.deletes = False
        await self.delete_conversation(run.conversation_id, now=NOW)
        return None


class Vanishing(MemoryConversationStore):
    """A store whose conversation is deleted the next time a run is read from.

    What a conversation deleted **under a watcher** looks like from above, at
    the one moment that is hard to arrange otherwise: the ownership check has
    passed, the events have been read, and by the time anybody asks how the run
    ended there is no run. It is armed by the test (``deletes``) and fires
    once, so the writes of the turn itself are untouched.
    """

    def __init__(self) -> None:
        super().__init__()
        self.deletes = False

    async def events_of(
        self, run_id: uuid.UUID, *, after: int = 0, upto: int | None = None
    ) -> tuple[object, ...]:
        read = await super().events_of(run_id, after=after, upto=upto)
        run = await self.run_by_id(run_id)
        if self.deletes and run is not None:
            self.deletes = False
            await self.delete_conversation(run.conversation_id, now=NOW)
        return read


def types(blocks: Iterable[Sent]) -> list[str]:
    """The AG-UI type of each event, in order: what a test reads at a glance."""
    return [block.type for block in blocks if block.comment is None]


def ids(blocks: Iterable[Sent]) -> list[str | None]:
    return [block.id for block in blocks if block.comment is None]


def said(blocks: Iterable[Sent]) -> str:
    """The answer as those events build it: every text delta, joined."""
    return "".join(
        block.body["delta"]
        for block in blocks
        if block.comment is None and block.type == EventType.TEXT_MESSAGE_CONTENT.value
    )


STARTS = frozenset(
    {
        EventType.TEXT_MESSAGE_START.value,
        EventType.REASONING_MESSAGE_START.value,
        EventType.TOOL_CALL_START.value,
    }
)
ENDS = frozenset(
    {
        EventType.TEXT_MESSAGE_END.value,
        EventType.REASONING_MESSAGE_END.value,
        EventType.TOOL_CALL_END.value,
    }
)
TERMINAL = frozenset({EventType.RUN_FINISHED.value, EventType.RUN_ERROR.value})
"""The kinds the comparison below has to know about; every other is compared as it is."""


def sequence(blocks: Iterable[Sent]) -> list[tuple[object, ...]]:
    """What those events tell a client, with the documented no-ops taken out.

    Each event as everything a client builds a conversation out of: its kind,
    the message or the call it is about, the text it appends, the tool it
    names, a result's content and flag, the thread and run it names, and --
    for the event that ends a run -- the outcome, code and message that say
    how. Three things are dropped, and only those three: a ``*_START`` for a
    message or a call that is already open, a ``*_END`` for one that is not
    open, and **the first of two adjacent terminal events**. They are what
    re-attaching costs -- the bracket around the cut is derived again, and
    re-attaching at or past the end repeats the ending -- and what a client
    is told to read as nothing (``docs/specs/wire.md``). A terminal event
    followed by anything else is kept, so a stream that ended and then went
    on saying things still fails.
    """
    read: list[tuple[object, ...]] = []
    open_ids: set[object] = set()
    for block in blocks:
        if block.comment is not None:
            continue
        kind, body = block.type, block.body
        named = body.get("messageId") if "toolCallId" not in body else body.get("toolCallId")
        if kind in STARTS:
            if named in open_ids:
                continue
            open_ids.add(named)
        if kind in ENDS:
            if named not in open_ids:
                continue
            open_ids.discard(named)
        read.append(
            (
                kind,
                named,
                body.get("delta"),
                body.get("toolCallName"),
                body.get("parentMessageId"),
                body.get("messageId"),
                body.get("content"),
                body.get("metadata"),
                body.get("threadId"),
                body.get("runId"),
                body.get("outcome"),
                body.get("code"),
                body.get("message"),
            )
        )
    return [
        one
        for index, one in enumerate(read)
        if one[0] not in TERMINAL or index == len(read) - 1 or read[index + 1][0] not in TERMINAL
    ]


def two_stretches(text: str) -> list[Step]:
    """An engine that thinks, says half of it, thinks again and says the rest.

    One stretch of thinking cannot show an id shared between two of them, and
    a turn that thinks only at the beginning cannot show a bracket closed in
    the middle of an answer.
    """
    half = len(text) // 2
    return [
        AnswerStarted(),
        AnswerReasoningDelta(text="Let me think. "),
        AnswerTextDelta(text=text[:half]),
        AnswerReasoningDelta(text="And again. "),
        AnswerTextDelta(text=text[half:]),
        AnswerCompleted(parts=text_parts(text)),
    ]


GITHUB = ToolServerConfig(
    id="github", url="https://github.example.test/mcp/", secret_env="ROBINAUTS_GITHUB_TOKEN"
)

SEARCH_CALL = ("toolu_01", "github__search_repositories", {"q": "robinauts"})


def tooled() -> dict[str, Any]:
    """What ``served`` is handed for an agent with the ``github`` server.

    The server lists ``SEARCH`` and answers it with a result; a test that
    wants an error result changes the fake's answer.
    """
    tools = MemoryToolServers()
    tools.serving(GITHUB.id, SEARCH)
    tools.answering(GITHUB.id, SEARCH.name, ToolResult("found 3"))
    return {
        "definition": agent_definition(tools=(GITHUB.id,)),
        "servers": {GITHUB.id: GITHUB},
        "tools": tools,
    }


def refusal(response: httpx.Response) -> tuple[int, dict[str, str], object]:
    """Everything a client can see of an answer: the status, the headers, the body."""
    return response.status_code, dict(response.headers), response.json()


# --- a whole turn -------------------------------------------------------------


@asyncio_test
async def test_a_turn_is_answered_with_the_ag_ui_stream_of_its_run() -> None:
    async with served(*says(ANSWER, pieces=2)) as it:
        answered = await it.client.post(
            "/api/turns", json={"agent_id": AGENT, "text": QUESTION}, headers=WRITE
        )

    assert answered.status_code == 200
    assert answered.headers["content-type"].startswith(SSE_MEDIA_TYPE)
    sent = events(answered.text)
    assert types(sent) == [
        EventType.RUN_STARTED.value,
        EventType.TEXT_MESSAGE_START.value,
        EventType.TEXT_MESSAGE_CONTENT.value,
        EventType.TEXT_MESSAGE_CONTENT.value,
        EventType.TEXT_MESSAGE_END.value,
        EventType.RUN_FINISHED.value,
    ]
    assert said(sent) == ANSWER
    # The thread every event names is the conversation the headers name.
    assert sent[0].body["threadId"] == answered.headers[CONVERSATION_ID_HEADER]
    assert sent[0].body["runId"] == answered.headers[RUN_ID_HEADER]


@asyncio_test
async def test_an_id_marks_the_last_event_derived_from_one_platform_event() -> None:
    """``id:`` is the platform's numbering, and it says what has been **sent**.

    One platform event can become more than one wire event -- the brackets
    around thinking are derived here -- and the platform numbered none of the
    derived ones. So the id goes on the last of each group: it means
    "everything derived from the run's events up to this position has gone
    out", which is exactly what a client re-attaching at it needs it to mean.
    """
    async with served(*says(ANSWER, pieces=3, reasoning="Let me think.")) as it:
        answered = await it.client.post(
            "/api/turns", json={"agent_id": AGENT, "text": QUESTION}, headers=WRITE
        )
        run_id = uuid.UUID(answered.headers[RUN_ID_HEADER])
        stored = await it.wiring.store.last_position(run_id)

    blocks = events(answered.text)
    assert [(block.type, block.id) for block in blocks] == [
        (EventType.RUN_STARTED.value, "1"),
        (EventType.TEXT_MESSAGE_START.value, "2"),
        # Derived: the platform published one reasoning delta at position 3.
        (EventType.REASONING_MESSAGE_START.value, None),
        (EventType.REASONING_MESSAGE_CONTENT.value, "3"),
        # Derived: the thinking is closed by the first text of position 4.
        (EventType.REASONING_MESSAGE_END.value, None),
        (EventType.TEXT_MESSAGE_CONTENT.value, "4"),
        (EventType.TEXT_MESSAGE_CONTENT.value, "5"),
        (EventType.TEXT_MESSAGE_CONTENT.value, "6"),
        (EventType.TEXT_MESSAGE_END.value, "7"),
        (EventType.RUN_FINISHED.value, "8"),
    ]
    # Every position of the run, once, in order, and nothing else numbered.
    numbers = [int(seen) for seen in ids(blocks) if seen is not None]
    assert numbers == list(range(1, stored + 1))


@asyncio_test
async def test_a_tool_round_is_the_calls_events_the_results_and_nothing_for_the_tool_message() -> (
    None
):
    """One round on the wire (``docs/specs/wire.md``): the call inside the
    answer under its own id, the result as it lands naming the tool message,
    and no text message for the tool message -- announced or completed, it
    sends nothing, so those two positions carry no ``id:`` and a client
    re-attaching at the last id it saw is replayed nothing it had."""
    async with served(*calls(SEARCH_CALL, text="Let me look."), **tooled()) as it:
        it.wiring.agent.then(*says("Found three.", pieces=1))
        it.wiring.tools.answering(GITHUB.id, SEARCH.name, ToolResult("no such repo", is_error=True))
        answered = await it.client.post(
            "/api/turns", json={"agent_id": AGENT, "text": QUESTION}, headers=WRITE
        )
        run_id = uuid.UUID(answered.headers[RUN_ID_HEADER])
        stored = await it.wiring.store.last_position(run_id)
        question, calling, results, done = await stored_messages(
            it.wiring.store, it.wiring.agent.asked[-1].history[0].conversation_id
        )

    blocks = events(answered.text)
    assert [(block.type, block.id) for block in blocks] == [
        (EventType.RUN_STARTED.value, "1"),
        (EventType.TEXT_MESSAGE_START.value, "2"),
        (EventType.TEXT_MESSAGE_CONTENT.value, "3"),
        (EventType.TOOL_CALL_START.value, "4"),
        (EventType.TOOL_CALL_ARGS.value, "5"),
        (EventType.TOOL_CALL_ARGS.value, "6"),
        (EventType.TOOL_CALL_END.value, "7"),
        (EventType.TEXT_MESSAGE_END.value, "8"),
        # Position 9 is the tool message announced, which sends nothing.
        (EventType.TOOL_CALL_RESULT.value, "10"),
        # Position 11 is the tool message completed: nothing again.
        (EventType.TEXT_MESSAGE_START.value, "12"),
        (EventType.TEXT_MESSAGE_CONTENT.value, "13"),
        (EventType.TEXT_MESSAGE_END.value, "14"),
        (EventType.RUN_FINISHED.value, "15"),
    ]
    assert stored == 15
    start, *args, end = blocks[3:7]
    assert start.body == {
        "type": EventType.TOOL_CALL_START.value,
        "toolCallId": "toolu_01",
        "toolCallName": "github__search_repositories",
        "parentMessageId": str(calling.id),
    }
    assert "".join(block.body["delta"] for block in args) == '{"q": "robinauts"}'
    assert end.body == {"type": EventType.TOOL_CALL_END.value, "toolCallId": "toolu_01"}
    assert blocks[8].body == {
        "type": EventType.TOOL_CALL_RESULT.value,
        "messageId": str(results.id),
        "toolCallId": "toolu_01",
        "content": "no such repo",
        "role": "tool",
        "metadata": {"isError": True},
    }
    assert (calling.id, results.id, done.id) == (
        calling.id,
        results.parent_id and results.id,
        done.id,
    )
    assert results.parent_id == calling.id and done.parent_id == results.id
    assert blocks[9].body["messageId"] == str(done.id)
    # The result's text is on the wire once, as content, and nowhere else.
    assert answered.text.count("no such repo") == 1


@pytest.mark.parametrize("form", ["last-event-id", "after"])
@pytest.mark.parametrize("ending", [RunState.FINISHED, RunState.CANCELLED])
@pytest.mark.parametrize("shape", ["thinking", "tools"])
@asyncio_test
async def test_dropping_anywhere_and_re_attaching_gives_the_whole_stream_once(
    ending: RunState, form: str, shape: str
) -> None:
    """The property re-attaching has to have, asked at **every** point of a turn.

    A client drops after each block in turn -- the ones that carry an ``id:``
    and the ones the wire derived, which carry none -- and re-attaches with the
    last id it saw. What it had, plus what it is then sent, must be the stream
    it would have received had it never dropped: the same events, the same
    message ids, the same deltas, in the same order.

    Three differences are allowed, and each is one a client is documented to
    read as a no-op (``docs/specs/wire.md``): a ``*_START`` for a message it
    already holds open and a ``*_END`` for one it does not, which are what a
    drop on either side of a derived bracket costs, and a second terminal
    event, which is what re-attaching at the very end gives. Everything else --
    a delta lost, a delta twice, a thinking block never closed -- fails here.

    The turn thinks **twice**, because one stretch would not show an id shared
    between two of them, and streams its answer around them. It is asked of a
    run that **finished** and of one that was **cancelled**, since how a run
    ends is part of what a client is owed, and through both ways of saying
    where to carry on from. And of a turn with a **tool round** before that
    answer (``shape``), so that the cuts fall inside a call's arguments, on
    the two positions a tool message sends nothing for, and on a result.
    """
    gate = Gate()
    steps = two_stretches(ANSWER)
    if ending is RunState.CANCELLED:
        steps.insert(3, gate)
    changes: dict[str, Any] = {}
    reached = 4
    if shape == "tools":
        changes = tooled()
        steps, second = list(calls(SEARCH_CALL, text="Let me look.")), steps
        reached = 4 + 10  # the round before adds ten positions; the run started is shared
    async with served(*steps, **changes) as it:
        if shape == "tools":
            it.wiring.agent.then(*second)
        started = await it.begun()
        if ending is RunState.CANCELLED:
            await stream_reached(it.wiring.store, started.run.id, reached)
            await it.wiring.turns.cancel(it.user, started.run.id)
            gate.open()
        await settled(it.wiring, started.run)
        run_id = started.run.id

        # The stream an unbroken client would have received, read back whole.
        whole = await it.client.get(f"/api/runs/{run_id}/events")
        blocks = events(whole.text)
        rest = []
        for cut in range(len(blocks)):
            seen = [block.id for block in blocks[: cut + 1] if block.id is not None]
            asked: dict[str, Any] = {}
            if seen and form == "last-event-id":
                asked = {"headers": {"last-event-id": seen[-1]}}
            elif seen:
                asked = {"params": {"after": int(seen[-1])}}
            rest.append(await it.client.get(f"/api/runs/{run_id}/events", **asked))

    assert (await it.wiring.store.run_by_id(run_id)).state is ending
    # Not vacuous: derived brackets, streamed text and an ending, all cut into.
    assert EventType.REASONING_MESSAGE_END.value in types(blocks)
    assert types(blocks)[-1] in {EventType.RUN_FINISHED.value, EventType.RUN_ERROR.value}
    assert len(blocks) >= 7
    if shape == "tools":
        assert EventType.TOOL_CALL_RESULT.value in types(blocks)
        assert types(blocks).count(EventType.TOOL_CALL_ARGS.value) == 2
    whole_sequence = sequence(blocks)
    for cut, answered in enumerate(rest):
        carried_on = sequence(blocks[: cut + 1] + events(answered.text))
        assert carried_on == whole_sequence, f"dropping after block {cut}"
    # And the client that saw everything is not sent nothing: a stream that
    # merely closed is one an ``EventSource`` opens again.
    assert types(events(rest[-1].text)) == [types(blocks)[-1]]


@asyncio_test
async def test_re_attaching_inside_a_stretch_of_thinking_still_closes_it() -> None:
    """The stream that carries on knows what the one that dropped had open.

    Dropping on the last reasoning delta and re-attaching there is the case
    that has nothing in the slice to derive a bracket from: the first event
    past the position is the text that ends the thinking, and a stream that
    began with no state would send the text and never the
    ``REASONING_MESSAGE_END``. The client's thinking block would then stay open
    for ever. ``Watch.before`` and ``AguiMapper.seed`` are what stop that.
    """
    async with served(*two_stretches(ANSWER)) as it:
        whole = await it.client.post(
            "/api/turns", json={"agent_id": AGENT, "text": QUESTION}, headers=WRITE
        )
        run_id = whole.headers[RUN_ID_HEADER]
        thinking = next(
            block
            for block in events(whole.text)
            if block.type == EventType.REASONING_MESSAGE_CONTENT.value
        )

        carried_on = await it.client.get(
            f"/api/runs/{run_id}/events", headers={"last-event-id": thinking.id or ""}
        )

    sent = events(carried_on.text)
    assert thinking.id is not None
    assert types(sent)[0] == EventType.REASONING_MESSAGE_END.value
    # The same stretch the dropped stream opened: the id is derived from the
    # answer and the position, so both streams built it the same way.
    assert sent[0].body["messageId"] == thinking.body["messageId"]
    # It is closed and not re-opened: the seeded mapper knows it is open. The
    # start that does follow is the turn's **second** stretch, under an id of
    # its own.
    opened = [block for block in sent if block.type == EventType.REASONING_MESSAGE_START.value]
    assert [block.body["messageId"] for block in opened] != [thinking.body["messageId"]]
    assert len(opened) == 1


@asyncio_test
async def test_a_stream_that_could_not_be_seeded_ends_as_an_error_and_is_logged(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The seeding happens after the response has begun, so it may not escape.

    Whatever goes wrong there is a mistake of ours like any other inside a
    stream: the client is told as much as a 500 body says, and the whole of it
    goes to the log. A generator that simply stopped would leave a client
    waiting for a turn that was never coming.
    """

    def raising(self: object, event: object) -> None:
        raise RuntimeError("the mapping came apart")

    async with served(*says(ANSWER)) as it:
        started = await it.begun()
        await settled(it.wiring, started.run)
        monkeypatch.setattr("robinauts.api.agui.AguiMapper.of", raising)

        with caplog.at_level(logging.ERROR):
            broken = await it.client.get(f"/api/runs/{started.run.id}/events", params={"after": 2})

    sent = events(broken.text)
    assert broken.status_code == 200
    assert types(sent) == [EventType.RUN_ERROR.value]
    assert sent[0].body == {
        "type": EventType.RUN_ERROR.value,
        "message": GENERIC_DETAIL,
        "code": INTERNAL_CODE,
    }
    said_once = [record for record in caplog.records if "could not be served" in record.message]
    assert len(said_once) == 1
    # What went wrong is in the log and nowhere near the wire.
    assert "the mapping came apart" in caplog.text
    assert "the mapping came apart" not in broken.text


@asyncio_test
async def test_the_stream_says_it_is_not_to_be_kept_or_buffered() -> None:
    async with served(*says(ANSWER)) as it:
        answered = await it.client.post(
            "/api/turns", json={"agent_id": AGENT, "text": QUESTION}, headers=WRITE
        )

    assert answered.headers["cache-control"] == "no-store"
    assert answered.headers["x-accel-buffering"] == "no"
    # And the headers every answer of this deployment carries.
    assert answered.headers["x-content-type-options"] == "nosniff"
    assert answered.headers["referrer-policy"] == "same-origin"


@asyncio_test
async def test_a_turn_in_a_conversation_hangs_under_what_it_was_told_to() -> None:
    async with served(*says(ANSWER)) as it:
        first = await it.client.post(
            "/api/turns", json={"agent_id": AGENT, "text": QUESTION}, headers=WRITE
        )
        conversation_id = uuid.UUID(first.headers[CONVERSATION_ID_HEADER])
        stored = await stored_messages(it.wiring.store, conversation_id)
        it.wiring.agent.steps = says("And that is all.")

        again = await it.client.post(
            f"/api/conversations/{conversation_id}/turns",
            json={"text": "And?", "parent_id": str(stored[-1].id)},
            headers=WRITE,
        )
        messages = await stored_messages(it.wiring.store, conversation_id)

    assert again.status_code == 200
    assert types(events(again.text))[-1] == EventType.RUN_FINISHED.value
    assert [message.parts[0].text for message in messages] == [  # type: ignore[union-attr]
        QUESTION,
        ANSWER,
        "And?",
        "And that is all.",
    ]


@asyncio_test
async def test_a_regeneration_answers_the_same_question_again() -> None:
    async with served(*says(ANSWER)) as it:
        first = await it.client.post(
            "/api/turns", json={"agent_id": AGENT, "text": QUESTION}, headers=WRITE
        )
        conversation_id = uuid.UUID(first.headers[CONVERSATION_ID_HEADER])
        answered = (await stored_messages(it.wiring.store, conversation_id))[-1]
        it.wiring.agent.steps = says("Someone who plays fairly.")

        again = await it.client.post(
            f"/api/conversations/{conversation_id}/turns",
            json={"regenerate": str(answered.id)},
            headers=WRITE,
        )
        messages = await stored_messages(it.wiring.store, conversation_id)

    assert again.status_code == 200
    assert again.headers[RUN_ID_HEADER] != first.headers[RUN_ID_HEADER]
    # The new answer is a sibling of the old one, under the same question.
    assert [message.parent_id for message in messages[1:]] == [
        messages[0].id,
        messages[0].id,
    ]


@asyncio_test
async def test_thinking_is_streamed_and_no_message_holds_any_of_it() -> None:
    """Shown as it arrives, collapsed, and stored nowhere (``poc-scope.md``)."""
    async with served(*says(ANSWER, reasoning="Let me think.")) as it:
        answered = await it.client.post(
            "/api/turns", json={"agent_id": AGENT, "text": QUESTION}, headers=WRITE
        )
        conversation_id = uuid.UUID(answered.headers[CONVERSATION_ID_HEADER])
        messages = await stored_messages(it.wiring.store, conversation_id)

    thinking = [
        block
        for block in events(answered.text)
        if block.type == EventType.REASONING_MESSAGE_CONTENT.value
    ]
    assert [block.body["delta"] for block in thinking] == ["Let me think."]
    assert all(
        not isinstance(part, ReasoningPart) for message in messages for part in message.parts
    )
    assert "Let me think." not in str([message.parts for message in messages])


# --- re-attaching -------------------------------------------------------------


@asyncio_test
async def test_re_attaching_after_a_position_gives_exactly_the_rest() -> None:
    async with served(*says(ANSWER, pieces=2)) as it:
        whole = await it.client.post(
            "/api/turns", json={"agent_id": AGENT, "text": QUESTION}, headers=WRITE
        )
        run_id = whole.headers[RUN_ID_HEADER]

        rest = await it.client.get(f"/api/runs/{run_id}/events", params={"after": 2})

    assert rest.status_code == 200
    assert rest.headers[RUN_ID_HEADER] == run_id
    assert rest.headers[CONVERSATION_ID_HEADER] == whole.headers[CONVERSATION_ID_HEADER]
    # Exactly what a client that had seen the first two positions has not seen.
    assert ids(events(rest.text)) == [
        seen for seen in ids(events(whole.text)) if seen is not None and int(seen) > 2
    ]


@asyncio_test
async def test_re_attaching_with_the_last_event_id_gives_the_same_thing() -> None:
    """A browser's ``EventSource`` sends it by itself; nothing else is needed."""
    async with served(*says(ANSWER, pieces=2)) as it:
        whole = await it.client.post(
            "/api/turns", json={"agent_id": AGENT, "text": QUESTION}, headers=WRITE
        )
        run_id = whole.headers[RUN_ID_HEADER]

        asked = await it.client.get(f"/api/runs/{run_id}/events", params={"after": 2})
        by_header = await it.client.get(
            f"/api/runs/{run_id}/events", headers={"last-event-id": "2"}
        )

    assert by_header.status_code == 200
    assert by_header.text == asked.text


@asyncio_test
async def test_re_attaching_past_the_end_of_a_finished_run_says_it_finished() -> None:
    """Nothing is left to send, and the record still says how the run ended.

    A client that simply closed would ask again, and one told ``gone`` would
    show a failure over an answer that is complete (``docs/specs/runs.md``).
    """
    async with served(*says(ANSWER)) as it:
        whole = await it.client.post(
            "/api/turns", json={"agent_id": AGENT, "text": QUESTION}, headers=WRITE
        )
        run_id = uuid.UUID(whole.headers[RUN_ID_HEADER])
        last = await it.wiring.store.last_position(run_id)

        after = await it.client.get(f"/api/runs/{run_id}/events", params={"after": last})

    sent = events(after.text)
    assert types(sent) == [EventType.RUN_FINISHED.value]
    assert sent[0].body == {
        "type": EventType.RUN_FINISHED.value,
        "threadId": whole.headers[CONVERSATION_ID_HEADER],
        "runId": str(run_id),
    }
    # It is not numbered: the platform numbered nothing here, and a client that
    # re-attaches again asks from the last real position.
    assert sent[0].id is None


@asyncio_test
async def test_re_attaching_past_the_end_of_a_cancelled_run_says_it_was_cancelled() -> None:
    """The same reading of the record, for a run that somebody stopped."""
    gate = Gate()
    async with served(gate, *says(ANSWER)) as it:
        started = await it.begun()
        await it.wiring.turns.cancel(it.user, started.run.id)
        gate.open()
        await settled(it.wiring, started.run)
        last = await it.wiring.store.last_position(started.run.id)

        after = await it.client.get(f"/api/runs/{started.run.id}/events", params={"after": last})

    sent = events(after.text)
    assert types(sent) == [EventType.RUN_FINISHED.value]
    assert sent[0].body == {
        "type": EventType.RUN_FINISHED.value,
        "threadId": str(started.conversation.id),
        "runId": str(started.run.id),
        # A cancellation is not a failure, and AG-UI says so in the outcome.
        "outcome": {"type": "cancelled"},
    }
    assert sent[0].id is None


@asyncio_test
async def test_a_run_that_goes_while_it_is_being_watched_says_it_is_gone() -> None:
    """The one thing ``gone`` is for: the rows are not there any more.

    A conversation deleted **before** the request is a 404 before the stream;
    this is the deletion landing under the watcher, which the store below makes
    happen at the one moment it can.
    """
    store = Vanishing()
    async with served(*says(ANSWER), store=store) as it:
        whole = await it.client.post(
            "/api/turns", json={"agent_id": AGENT, "text": QUESTION}, headers=WRITE
        )
        run_id = uuid.UUID(whole.headers[RUN_ID_HEADER])
        last = await store.last_position(run_id)
        store.deletes = True

        after = await it.client.get(f"/api/runs/{run_id}/events", params={"after": last})

    sent = events(after.text)
    assert types(sent) == [EventType.RUN_ERROR.value]
    assert sent[0].body == {
        "type": EventType.RUN_ERROR.value,
        "message": GONE_DETAIL,
        "code": GONE_CODE,
    }
    assert sent[0].id is None


@asyncio_test
async def test_a_run_that_goes_between_the_two_ownership_checks_is_gone_and_not_a_fault(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The watcher refusing where the route did not is a deletion, not a bug.

    The route settles ownership before it opens a stream and the watcher
    settles it again for itself; a conversation deleted in that gap makes the
    second raise. It is ``gone`` -- the run really is -- and **nothing is
    logged at ERROR**, which is what a stream that met a fault of ours does.
    """
    store = VanishingRun()
    with caplog.at_level("ERROR"):
        async with served(*says(ANSWER), store=store) as it:
            whole = await it.client.post(
                "/api/turns", json={"agent_id": AGENT, "text": QUESTION}, headers=WRITE
            )
            run_id = uuid.UUID(whole.headers[RUN_ID_HEADER])
            store.deletes = True

            after = await it.client.get(f"/api/runs/{run_id}/events")

    sent = events(after.text)
    assert after.status_code == 200
    assert types(sent) == [EventType.RUN_ERROR.value]
    assert sent[0].body["code"] == GONE_CODE
    assert caplog.records == []
    assert sent[0].id is None


# --- refusals, before a byte of stream ----------------------------------------


@asyncio_test
async def test_a_strangers_run_and_a_run_that_is_not_there_answer_identically() -> None:
    """The one body for everything that is not there, headers included.

    Asked as an **identity**: a run in somebody else's conversation and a run
    that never existed must not differ in a status, a byte of a body or a
    header, since that difference is the whole of what an attacker wants from
    an id.
    """
    async with served(*says(ANSWER)) as it:
        theirs = await it.wiring.turns.begin(SOMEBODY_ELSE, agent_id=AGENT, text=QUESTION)
        await settled(it.wiring, theirs.run)

        elsewhere = await it.client.get(f"/api/runs/{theirs.run.id}/events")
        missing = await it.client.get(f"/api/runs/{NOWHERE}/events")

    assert refusal(elsewhere) == refusal(missing)
    assert missing.status_code == 404
    assert missing.json() == {"error": NOT_FOUND_ERROR, "detail": NOT_FOUND_DETAIL}
    # Not a stream at all: a refusal is a status, and it comes first.
    assert missing.headers["content-type"] == "application/json"
    assert RUN_ID_HEADER not in missing.headers


@asyncio_test
async def test_a_second_turn_while_one_is_going_is_refused() -> None:
    """One run at a time; its author cancels the first and asks again."""
    gate = Gate()
    async with served(gate, *says(ANSWER)) as it:
        started = await it.begun()

        refused = await it.client.post(
            f"/api/conversations/{started.conversation.id}/turns",
            json={"text": "And?"},
            headers=WRITE,
        )
        gate.open()
        await settled(it.wiring, started.run)

    assert refused.status_code == 409
    assert refused.json()["error"] == "RunAlreadyActiveError"


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"text": "And?", "regenerate": str(NOWHERE)},
        {"parent_id": str(NOWHERE)},
        {"regenerate": str(NOWHERE), "parent_id": str(NOWHERE)},
    ],
)
@asyncio_test
async def test_a_turn_that_is_neither_form_or_both_is_refused(body: dict[str, str]) -> None:
    async with served(*says(ANSWER)) as it:
        started = await it.begun()
        await settled(it.wiring, started.run)

        refused = await it.client.post(
            f"/api/conversations/{started.conversation.id}/turns", json=body, headers=WRITE
        )

    assert refused.status_code == 422
    assert refused.json() == {"error": "InvalidValueError", "detail": ONE_FORM}


@asyncio_test
async def test_a_message_with_nothing_in_it_is_refused() -> None:
    async with served(*says(ANSWER)) as it:
        empty = await it.client.post(
            "/api/turns", json={"agent_id": AGENT, "text": ""}, headers=WRITE
        )
        spaces = await it.client.post(
            "/api/turns", json={"agent_id": AGENT, "text": "   "}, headers=WRITE
        )

    assert empty.status_code == spaces.status_code == 422
    assert empty.json()["error"] == "InvalidValueError"
    assert spaces.json()["detail"] == "a message has something in it"


def test_how_long_a_message_may_be_is_the_records_own_bound() -> None:
    """Said in the schema, so a client holds itself to it (``RenameRequest``)."""
    bounds = TurnRequest.model_fields["text"].metadata

    assert MAX_MESSAGE_CHARS in [getattr(one, "max_length", None) for one in bounds]
    assert 1 in [getattr(one, "min_length", None) for one in bounds]


def test_every_field_of_a_new_chat_carries_the_bound_of_the_record_it_becomes() -> None:
    """An agent's id is as long as a configured id may be, and no longer.

    A field with no bound is a field whose length is the sender's, and this
    one is a key into what the operator configured.
    """
    agent = NewChatRequest.model_fields["agent_id"].metadata
    model = NewChatRequest.model_fields["model_id"].metadata
    text = NewChatRequest.model_fields["text"].metadata

    assert MAX_CONFIG_ID_CHARS in [getattr(one, "max_length", None) for one in agent]
    assert 1 in [getattr(one, "min_length", None) for one in agent]
    assert MAX_CONFIG_ID_CHARS in [getattr(one, "max_length", None) for one in model]
    assert 1 in [getattr(one, "min_length", None) for one in model]
    assert MAX_MESSAGE_CHARS in [getattr(one, "max_length", None) for one in text]


@asyncio_test
async def test_an_agent_id_longer_than_one_could_be_is_refused_by_the_schema() -> None:
    async with served(*says(ANSWER)) as it:
        refused = await it.client.post(
            "/api/turns",
            json={"agent_id": "a" * (MAX_CONFIG_ID_CHARS + 1), "text": QUESTION},
            headers=WRITE,
        )

    assert refused.status_code == 422
    assert refused.json() == {
        "error": "InvalidValueError",
        "detail": f"body.agent_id: {UNREADABLE_RULES['string_too_long']}",
    }


@asyncio_test
async def test_an_agent_this_deployment_has_not_got_is_not_there() -> None:
    async with served(*says(ANSWER)) as it:
        refused = await it.client.post(
            "/api/turns", json={"agent_id": "nobody", "text": QUESTION}, headers=WRITE
        )

    assert refused.status_code == 404
    assert refused.json() == {"error": NOT_FOUND_ERROR, "detail": NOT_FOUND_DETAIL}


@asyncio_test
async def test_a_new_chat_runs_on_the_model_it_names() -> None:
    """Stored on the conversation, copied onto the run, and handed to the engine."""
    async with served(*says(ANSWER)) as it:
        answered = await it.client.post(
            "/api/turns",
            json={"agent_id": AGENT, "model_id": OTHER_MODEL, "text": QUESTION},
            headers=WRITE,
        )
        conversation = await it.wiring.store.conversation_by_id(
            uuid.UUID(answered.headers[CONVERSATION_ID_HEADER])
        )
        run = await it.wiring.store.run_by_id(uuid.UUID(answered.headers[RUN_ID_HEADER]))

    assert answered.status_code == 200
    assert conversation is not None and conversation.model == OTHER_MODEL
    assert run is not None and run.model == OTHER_MODEL
    assert [asked.model for asked in it.wiring.agent.asked] == [OTHER_MODEL]


@pytest.mark.parametrize("body", [{}, {"model_id": None}])
@asyncio_test
async def test_a_new_chat_that_names_no_model_runs_on_the_agents_default(
    body: dict[str, None],
) -> None:
    async with served(*says(ANSWER)) as it:
        answered = await it.client.post(
            "/api/turns", json={"agent_id": AGENT, "text": QUESTION, **body}, headers=WRITE
        )
        conversation = await it.wiring.store.conversation_by_id(
            uuid.UUID(answered.headers[CONVERSATION_ID_HEADER])
        )

    assert answered.status_code == 200
    assert conversation is not None and conversation.model == MODEL
    assert [asked.model for asked in it.wiring.agent.asked] == [MODEL]


@asyncio_test
async def test_a_model_this_deployment_has_not_got_is_refused_by_name() -> None:
    """422 under its own name, with nothing of the id, and before anything is written."""
    async with served(*says(ANSWER)) as it:
        refused = await it.client.post(
            "/api/turns",
            json={"agent_id": AGENT, "model_id": "haiku", "text": QUESTION},
            headers=WRITE,
        )
        page = await it.wiring.store.conversations_of(it.user.id, limit=10)

    assert refused.status_code == 422
    assert refused.json() == {"error": "UnknownModelError", "detail": NOT_OFFERED_DETAIL}
    assert page.conversations == ()
    assert not it.wiring.agent.asked


@asyncio_test
async def test_an_agent_that_is_not_there_is_looked_for_before_the_model() -> None:
    async with served(*says(ANSWER)) as it:
        refused = await it.client.post(
            "/api/turns",
            json={"agent_id": "nobody", "model_id": "haiku", "text": QUESTION},
            headers=WRITE,
        )

    assert refused.status_code == 404
    assert refused.json() == {"error": NOT_FOUND_ERROR, "detail": NOT_FOUND_DETAIL}


@pytest.mark.parametrize("turn", ["continue", "edit", "regenerate"])
@asyncio_test
async def test_a_conversation_whose_model_was_removed_is_not_answered(turn: str) -> None:
    """The model is the conversation's, and one no longer offered refuses the turn.

    409 under its own name -- the conversation stands in the way, not the
    request -- and not an answer from some other model: whoever reads it has
    to know which model answered. A continuation, an edit and a regeneration
    alike.
    """
    async with served(*says(ANSWER)) as it:
        started = await it.begun()
        await settled(it.wiring, started.run)
        question, answer = await stored_messages(it.wiring.store, started.conversation.id)
        # What a restart without that model leaves: the row still names it.
        await it.wiring.store.set_model(started.conversation.id, "retired", now=NOW)
        body = {
            "continue": {"text": "And?", "parent_id": str(answer.id)},
            "edit": {"text": "Rather?", "parent_id": question.parent_id},
            "regenerate": {"regenerate": str(answer.id)},
        }[turn]

        refused = await it.client.post(
            f"/api/conversations/{started.conversation.id}/turns", json=body, headers=WRITE
        )

    assert refused.status_code == 409
    assert refused.json() == {"error": "ModelNotOfferedError", "detail": NO_LONGER_OFFERED_DETAIL}
    assert len(it.wiring.agent.asked) == 1


@asyncio_test
async def test_a_strangers_conversation_on_a_removed_model_is_not_there() -> None:
    """Whose it is is decided before its model, so nothing about it is told."""
    async with served(*says(ANSWER)) as it:
        theirs = conversation(id=uuid.uuid4(), owner_id=SOMEBODY_ELSE.id, model="retired")
        await it.wiring.store.add_conversation(theirs)

        elsewhere = await it.client.post(
            f"/api/conversations/{theirs.id}/turns", json={"text": "And?"}, headers=WRITE
        )
        missing = await it.client.post(
            f"/api/conversations/{NOWHERE}/turns", json={"text": "And?"}, headers=WRITE
        )

    assert refusal(elsewhere) == refusal(missing)
    assert elsewhere.status_code == 404
    assert elsewhere.json() == {"error": NOT_FOUND_ERROR, "detail": NOT_FOUND_DETAIL}


@asyncio_test
async def test_a_model_id_longer_than_one_could_be_is_refused_by_the_schema() -> None:
    async with served(*says(ANSWER)) as it:
        refused = await it.client.post(
            "/api/turns",
            json={"agent_id": AGENT, "model_id": "m" * (MAX_CONFIG_ID_CHARS + 1), "text": QUESTION},
            headers=WRITE,
        )

    assert refused.status_code == 422
    assert refused.json() == {
        "error": "InvalidValueError",
        "detail": f"body.model_id: {UNREADABLE_RULES['string_too_long']}",
    }


@asyncio_test
async def test_a_turn_in_a_conversation_names_no_model() -> None:
    """The conversation's model is moved by its own route, never by a turn.

    So ``model_id`` is a field a turn's body does not know, and is refused as
    one -- the same 422, and the same sentence, as any other.
    """
    async with served(*says(ANSWER)) as it:
        started = await it.begun()
        await settled(it.wiring, started.run)

        refused = await it.client.post(
            f"/api/conversations/{started.conversation.id}/turns",
            json={"text": "And?", "parent_id": None, "model_id": OTHER_MODEL},
            headers=WRITE,
        )
        kept = await it.wiring.store.conversation_by_id(started.conversation.id)

    assert refused.status_code == 422
    assert refused.json() == {"error": "InvalidValueError", "detail": f"body: {UNKNOWN_FIELD}"}
    assert kept is not None and kept.model == MODEL


@asyncio_test
async def test_a_field_nobody_knows_is_refused_rather_than_ignored() -> None:
    async with served(*says(ANSWER)) as it:
        refused = await it.client.post(
            "/api/turns",
            json={"agent_id": AGENT, "text": QUESTION, "history": []},
            headers=WRITE,
        )

    assert refused.status_code == 422
    assert "history" not in refused.text


@asyncio_test
async def test_a_turn_sent_from_another_site_is_refused() -> None:
    async with served(*says(ANSWER)) as it:
        refused = await it.client.post(
            "/api/turns", json={"agent_id": AGENT, "text": QUESTION}, headers=CROSS_SITE
        )

    assert refused.status_code == 403
    assert refused.json()["error"] == "CrossSiteRequestError"


@asyncio_test
async def test_neither_route_is_served_without_a_session() -> None:
    async with served(*says(ANSWER)) as it:
        it.client.cookies.clear()

        begun = await it.client.post(
            "/api/turns", json={"agent_id": AGENT, "text": QUESTION}, headers=WRITE
        )
        followed = await it.client.get(f"/api/runs/{NOWHERE}/events")

    assert begun.status_code == followed.status_code == 401
    assert begun.json()["error"] == "AuthenticationError"


# --- where to carry on from ---------------------------------------------------


@asyncio_test
async def test_two_ways_of_saying_where_to_carry_on_from_may_not_disagree() -> None:
    async with served(*says(ANSWER)) as it:
        whole = await it.client.post(
            "/api/turns", json={"agent_id": AGENT, "text": QUESTION}, headers=WRITE
        )
        run_id = whole.headers[RUN_ID_HEADER]

        refused = await it.client.get(
            f"/api/runs/{run_id}/events",
            params={"after": 1},
            headers={"last-event-id": "3"},
        )
        agreeing = await it.client.get(
            f"/api/runs/{run_id}/events",
            params={"after": 3},
            headers={"last-event-id": "3"},
        )

    assert refused.status_code == 422
    assert refused.json() == {"error": "InvalidValueError", "detail": POSITIONS_DISAGREE}
    assert agreeing.status_code == 200


@pytest.mark.parametrize("said", ["nowhere", "-1", "1.5", "²", "9007199254740992"])
@asyncio_test
async def test_a_last_event_id_that_is_no_position_is_refused_without_being_repeated(
    said: str,
) -> None:
    async with served(*says(ANSWER)) as it:
        whole = await it.client.post(
            "/api/turns", json={"agent_id": AGENT, "text": QUESTION}, headers=WRITE
        )

        refused = await it.client.get(
            f"/api/runs/{whole.headers[RUN_ID_HEADER]}/events",
            # As bytes, because a header is bytes on the wire and one of these
            # is a digit that is not an ASCII one -- which ``int`` would read.
            headers={"last-event-id": said.encode("latin-1")},
        )

    assert refused.status_code == 422
    assert refused.json() == {"error": "InvalidValueError", "detail": UNREADABLE_POSITION}
    assert said not in refused.text


@asyncio_test
async def test_a_last_event_id_longer_than_any_position_is_refused_before_it_is_read() -> None:
    """``int`` refuses a decimal of a few thousand digits, and that is a 500.

    The length of a header is the sender's to choose, so it is bounded by what
    this build could have sent (``MAX_POSITION_DIGITS``) before anything reads
    it as a number.
    """
    async with served(*says(ANSWER)) as it:
        whole = await it.client.post(
            "/api/turns", json={"agent_id": AGENT, "text": QUESTION}, headers=WRITE
        )

        refused = await it.client.get(
            f"/api/runs/{whole.headers[RUN_ID_HEADER]}/events",
            headers={"last-event-id": "9" * 5_000},
        )
        # A position of that many characters, written out with the zeros a
        # sender may pad it with, is still read as the position it is.
        padded = f"{3:0{MAX_POSITION_DIGITS}d}"
        allowed = await it.client.get(
            f"/api/runs/{whole.headers[RUN_ID_HEADER]}/events",
            headers={"last-event-id": padded},
        )
        plainly = await it.client.get(
            f"/api/runs/{whole.headers[RUN_ID_HEADER]}/events", params={"after": 3}
        )

    assert refused.status_code == 422
    assert refused.json() == {"error": "InvalidValueError", "detail": UNREADABLE_POSITION}
    assert len(padded) == MAX_POSITION_DIGITS
    assert allowed.status_code == 200
    assert allowed.text == plainly.text


@asyncio_test
async def test_carrying_on_from_where_a_running_run_has_not_got_to_is_refused() -> None:
    """Nothing is there yet, and waiting for it would give up on a healthy run.

    A watcher would send nothing, sit out the whole silence a run is allowed
    and then say in the log that the run had gone quiet -- about a run that is
    answering, for a client that asked for a position it never saw.
    """
    gate = Gate()
    async with served(gate, *says(ANSWER), quiet_seconds=0.05, wait_seconds=0.05) as it:
        started = await it.begun()
        await stream_reached(it.wiring.store, started.run.id, 1)

        ahead = await it.client.get(f"/api/runs/{started.run.id}/events", params={"after": 99})
        at_the_end = await it.client.get(f"/api/runs/{started.run.id}/events", params={"after": 1})
        gate.open()
        await settled(it.wiring, started.run)

    assert ahead.status_code == 422
    assert ahead.json() == {"error": "InvalidValueError", "detail": POSITION_AHEAD}
    # **At** the last position it really has is not ahead of anything: that
    # stream is served. What ends it here is the ordinary one for a run that
    # says nothing -- this one is held at a gate, so it really is quiet -- and
    # the difference between the two is the point: one client asked for
    # something that is not there, the other for something that has not
    # happened yet.
    assert at_the_end.status_code == 200
    assert types(events(at_the_end.text)) == [EventType.RUN_ERROR.value]
    assert events(at_the_end.text)[0].body["code"] == QUIET_CODE


@asyncio_test
async def test_carrying_on_from_past_the_end_of_a_run_that_ended_is_not_refused() -> None:
    """There is nothing to wait for, so the outcome is the answer (not a 422)."""
    async with served(*says(ANSWER)) as it:
        started = await it.begun()
        await settled(it.wiring, started.run)

        ahead = await it.client.get(f"/api/runs/{started.run.id}/events", params={"after": 99})

    assert ahead.status_code == 200
    assert types(events(ahead.text)) == [EventType.RUN_FINISHED.value]


@asyncio_test
async def test_an_empty_last_event_id_is_a_client_that_has_seen_nothing() -> None:
    async with served(*says(ANSWER)) as it:
        whole = await it.client.post(
            "/api/turns", json={"agent_id": AGENT, "text": QUESTION}, headers=WRITE
        )
        run_id = whole.headers[RUN_ID_HEADER]

        from_nothing = await it.client.get(
            f"/api/runs/{run_id}/events", headers={"last-event-id": ""}
        )
        from_zero = await it.client.get(f"/api/runs/{run_id}/events")

    assert from_nothing.text == from_zero.text
    assert types(events(from_zero.text))[0] == EventType.RUN_STARTED.value


@asyncio_test
async def test_a_position_given_twice_is_refused_rather_than_read_once() -> None:
    async with served(*says(ANSWER)) as it:
        whole = await it.client.post(
            "/api/turns", json={"agent_id": AGENT, "text": QUESTION}, headers=WRITE
        )

        refused = await it.client.get(
            f"/api/runs/{whole.headers[RUN_ID_HEADER]}/events",
            params=[("after", "0"), ("after", "3")],
        )

    assert refused.status_code == 422
    assert refused.json()["detail"] == "query.after: given more than once"


@asyncio_test
async def test_a_position_outside_what_a_run_can_reach_is_refused() -> None:
    async with served(*says(ANSWER)) as it:
        whole = await it.client.post(
            "/api/turns", json={"agent_id": AGENT, "text": QUESTION}, headers=WRITE
        )

        refused = await it.client.get(
            f"/api/runs/{whole.headers[RUN_ID_HEADER]}/events",
            params={"after": 2**53},
        )

    assert refused.status_code == 422
    assert refused.json()["detail"].startswith("query.after")


# --- a stream that is still going ---------------------------------------------


@asyncio_test
async def test_a_quiet_run_is_told_about_rather_than_waited_on_for_ever() -> None:
    """The one 504 of the api, said as an event because the status is long gone."""
    gate = Gate()
    async with served(gate, *says(ANSWER), quiet_seconds=0.05, wait_seconds=0.05) as it:
        async with streaming(
            it.app,
            "POST",
            "/api/turns",
            headers=it.sending(**WRITE),
            body=b'{"agent_id": "assistant", "text": "What is a robinaut?"}',
        ) as stream:
            begun = await stream.next_block()
            ended = await stream.next_block()
            gate.open()

        run_id = uuid.UUID(stream.headers[RUN_ID_HEADER])
        await settled(it.wiring, (await it.wiring.store.run_by_id(run_id)))

    assert begun.type == EventType.RUN_STARTED.value
    assert ended.body == {
        "type": EventType.RUN_ERROR.value,
        "message": QUIET_RUN_DETAIL,
        "code": QUIET_CODE,
    }


@asyncio_test
async def test_a_quiet_stream_says_it_is_still_there(monkeypatch: pytest.MonkeyPatch) -> None:
    """A comment line, so that nothing in front of the deployment closes it."""
    monkeypatch.setattr("robinauts.api.stream_routes.HEARTBEAT_SECONDS", BEAT)
    gate = Gate()
    async with served(gate, *says(ANSWER)) as it:
        async with streaming(
            it.app,
            "POST",
            "/api/turns",
            headers=it.sending(**WRITE),
            body=b'{"agent_id": "assistant", "text": "What is a robinaut?"}',
        ) as stream:
            assert (await stream.next_block()).type == EventType.RUN_STARTED.value
            beat = await stream.next_block()
            gate.open()
            await _to_the_end(stream)

        run_id = uuid.UUID(stream.headers[RUN_ID_HEADER])
        await settled(it.wiring, (await it.wiring.store.run_by_id(run_id)))

    assert beat.comment == "keep-alive"
    assert beat.data == ""


@asyncio_test
async def test_a_client_that_goes_away_leaves_the_run_to_finish() -> None:
    """The stream is a view of the run, and closing it changes nothing.

    It also asks that the going away left nothing behind: the loop's exception
    handler is what says whether the task reading the watcher was cancelled and
    reaped, or dropped with something in it that nobody retrieved.
    """
    gate = Gate()
    unretrieved: list[object] = []
    asyncio.get_running_loop().set_exception_handler(
        lambda loop, context: unretrieved.append(context)
    )
    async with served(gate, *says(ANSWER)) as it:
        async with streaming(
            it.app,
            "POST",
            "/api/turns",
            headers=it.sending(**WRITE),
            body=b'{"agent_id": "assistant", "text": "What is a robinaut?"}',
        ) as stream:
            assert (await stream.next_block()).type == EventType.RUN_STARTED.value
            run_id = uuid.UUID(stream.headers[RUN_ID_HEADER])
            conversation_id = uuid.UUID(stream.headers[CONVERSATION_ID_HEADER])
            await stream.close()

        gate.open()
        await settled(it.wiring, (await it.wiring.store.run_by_id(run_id)))
        run = await it.wiring.store.run_by_id(run_id)
        messages = await stored_messages(it.wiring.store, conversation_id)

    assert run.state is RunState.FINISHED
    assert [message.parts[0].text for message in messages] == [QUESTION, ANSWER]  # type: ignore[union-attr]
    assert unretrieved == []


@asyncio_test
async def test_a_stream_that_ends_in_an_error_closes_the_thinking_first() -> None:
    """A thinking block nothing closed would be shown, open, for ever."""
    gate = Gate()
    steps = says(ANSWER, reasoning="Let me think.")
    steps.insert(2, gate)
    async with served(*steps, quiet_seconds=0.05, wait_seconds=0.05) as it:
        async with streaming(
            it.app,
            "POST",
            "/api/turns",
            headers=it.sending(**WRITE),
            body=b'{"agent_id": "assistant", "text": "What is a robinaut?"}',
        ) as stream:
            seen = await stream.upto(EventType.RUN_ERROR.value)
            gate.open()

        run_id = uuid.UUID(stream.headers[RUN_ID_HEADER])
        await settled(it.wiring, (await it.wiring.store.run_by_id(run_id)))

    assert types(seen) == [
        EventType.RUN_STARTED.value,
        EventType.TEXT_MESSAGE_START.value,
        EventType.REASONING_MESSAGE_START.value,
        EventType.REASONING_MESSAGE_CONTENT.value,
        EventType.REASONING_MESSAGE_END.value,
        EventType.RUN_ERROR.value,
    ]
    assert seen[-1].body["code"] == QUIET_CODE
    # Neither made-up event is numbered.
    assert [block.id for block in seen[-2:]] == [None, None]


# --- what is not served -------------------------------------------------------


@asyncio_test
async def test_a_stream_asked_for_before_start_up_says_nothing_about_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A deployment always has these services, so having none is a bug of ours.

    The same answer the conversation routes give
    (``robinauts.api.access.NOT_WIRED``): the generic body, and the whole of it
    in the log. **The turn is not begun first** -- both services are taken
    before anything is written -- so a POST that meets this creates no run.
    """
    deployment = wired()
    secret, _ = await open_session(deployment)

    with caplog.at_level("ERROR"):
        async with serving(deployment.sign_in) as client:
            client.cookies.set(session_cookie(secure=True), secret)

            begun = await client.post(
                "/api/turns", json={"agent_id": AGENT, "text": QUESTION}, headers=WRITE
            )
            followed = await client.get(f"/api/runs/{NOWHERE}/events")

    assert begun.status_code == followed.status_code == 500
    assert begun.json() == {"error": INTERNAL_ERROR, "detail": GENERIC_DETAIL}
    assert NOT_WIRED in caplog.text
    assert "app.state.watch" in caplog.text


def test_neither_streaming_path_is_in_the_openapi_document() -> None:
    """A streaming endpoint in it would have a client read the body as JSON.

    They are described in ``docs/specs/wire.md`` instead
    (``docs/specs/backend.md``), which is why the committed snapshot does not
    move when they change.
    """
    paths = openapi_document()["paths"]

    assert "/api/turns" not in paths
    assert "/api/conversations/{conversation_id}/turns" not in paths
    assert "/api/runs/{run_id}/events" not in paths
    # And by shape, so that a fourth one added later is caught too.
    assert [path for path in paths if path.endswith(("/turns", "/events"))] == []
    # Not vacuous: the plain half of the wire is in it.
    assert "/api/conversations/{conversation_id}" in paths


async def _to_the_end(stream: Streamed) -> list[Sent]:
    """Read the stream until it ends, so nothing is left half written."""
    return await stream.rest()
