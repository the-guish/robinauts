# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The conversation routes, over the real application and the fakes.

What is tested here is what the routes add to services that
``test_conversations_application.py`` and ``test_turn_lifecycle.py`` have
already proved: the shapes that go out, the statuses that come back, and the
two things a route can get wrong on its own -- letting somebody see what is
not theirs, and saying in a body what only a log should hold.

The application services are the real ones over the in-memory store
(``tests/turns.py``), and the executor and the signals under them are the real
adapters, so a cancellation that has to reach work in flight really does.
Nothing sleeps: the engine waits at a gate the test opens.

**Ownership is asked as an identity.** A conversation of somebody else's and a
conversation that never existed must answer the same status, the same body and
the same headers, byte for byte; the table at the bottom asks that of every
route that takes an id, which is the one way to be sure a later route cannot
tell the two apart by accident.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import get_args

import httpx
import pytest
from starlette.requests import Request

from aio import asyncio_test
from conversations import (
    AGENT,
    MODEL,
    OTHER_MODEL,
    answer,
    at,
    conversation,
    model_config,
    question,
)
from fakes import Gate, MemoryConversationStore, Step, says
from robinauts.legacy.api import (
    BODY_TOO_DEEP,
    BODY_TWICE,
    GENERIC_DETAIL,
    INTERNAL_ERROR,
    NOT_FOUND_DETAIL,
    NOT_FOUND_ERROR,
    NOT_OFFERED_DETAIL,
    NOT_WIRED,
    UNKNOWN_FIELD,
    UNREADABLE_RULES,
    SentBadEnd,
    SentKind,
    SentRole,
    allowed_methods,
    api_routes,
    create_api,
    read_once,
    session_cookie,
    status_of,
)
from robinauts.legacy.api.conversation_routes import PAGING
from robinauts.legacy.application import DEFAULT_PAGE, MAX_PAGE
from robinauts.legacy.core import message_to_data
from robinauts.legacy.domain import (
    FAULTED_RUN_STATES,
    SUPPORTED_PART_KINDS,
    SUPPORTED_ROLES,
    Conversation,
    InvalidValueError,
    Message,
    ModelConfig,
    PartKind,
    ReasoningPart,
    Role,
    RunState,
    TextPart,
    ToolCallPart,
    ToolResultPart,
    User,
)
from robinauts.legacy.ports import Snapshot
from turns import SOMEBODY_ELSE, Wiring, settled, submitted
from turns import wired as wired_services
from turns import written as stream_reached
from webapp import JSON, PUBLIC_URL, open_session, serving, wired

SNAPSHOT = Path(__file__).resolve().parents[2] / "openapi.json"
"""The committed document, for the two tests that are about what it promises."""

NOWHERE = uuid.UUID("99999999-9999-4999-8999-999999999999")
"""An id nothing here has: what "not there" is asked with."""

WRITE = {**JSON, "origin": PUBLIC_URL}
"""What a browser of this deployment sends with a write, and no more."""

CROSS_SITE = {**WRITE, "sec-fetch-site": "cross-site"}
"""The same write, from a page that is not ours."""

LONE_HALF = rb'{"title": "lone \ud800 half"}'
"""A title with half of a character in it: JSON reads it, no store can hold it."""

ANSWER = "Someone who plays fair."

NEW_TITLE = "What a robinaut is"

WRITES: tuple[tuple[str, str, dict[str, object] | None], ...] = (
    ("PATCH", "", {"title": NEW_TITLE}),
    ("PUT", "/model", {"model_id": OTHER_MODEL}),
    ("DELETE", "", None),
    ("POST", f"/runs/{NOWHERE}/cancel", None),
)
"""Every write here, as a method, what it adds to a conversation's path, and a body."""


class CountingStore(MemoryConversationStore):
    """The fake, with the one read whose cost is worth counting counted.

    ``conversation_snapshot`` reads every message of a conversation, the run
    in flight and each of its events, and decodes every document. What it
    costs is what makes "not yours" tell itself apart from "not there" by a
    clock, so a test asks how many times it happened rather than how long it
    took.
    """

    def __init__(self) -> None:
        super().__init__()
        self.snapshots = 0

    async def conversation_snapshot(self, conversation_id: uuid.UUID) -> Snapshot:
        self.snapshots += 1
        return await super().conversation_snapshot(conversation_id)


@dataclass(frozen=True, slots=True)
class Served:
    """The application, the services under it, and the person in the session."""

    client: httpx.AsyncClient
    wiring: Wiring
    user: User

    async def written(self, *messages: Message, **changes: object) -> Conversation:
        """A conversation of that person's, with those messages appended in order."""
        kept = conversation(owner_id=self.user.id, **changes)
        await self.wiring.store.add_conversation(kept)
        for message in messages:
            await self.wiring.store.append_message(
                message, message_to_data(message), now=message.created_at
            )
        return kept

    async def stored(self, conversation_id: uuid.UUID) -> Conversation | None:
        return await self.wiring.store.conversation_by_id(conversation_id)

    async def answering(self, *, text: str = "What is a robinaut?") -> Conversation:
        """A new conversation of theirs with a run in flight, begun and not executed."""
        started = await self.wiring.turns.start(self.user, agent_id=AGENT, text=text)
        return started.conversation


@asynccontextmanager
async def served(
    *steps: Step,
    store: MemoryConversationStore | None = None,
    models: dict[str, ModelConfig] | None = None,
) -> AsyncIterator[Served]:
    """The real application with a session open, over services on the fakes.

    The session is put in the credential store directly and its secret in the
    client's jar: signing in is ``tests/unit/test_api_auth_routes.py``'s
    subject, and the conversations belong to whoever that session names.
    """
    deployment = wired()
    secret, user = await open_session(deployment)
    services = wired_services(*steps, store=store, models=models)
    async with serving(
        deployment.sign_in,
        conversations=services.conversations,
        turns=services.turns,
    ) as client:
        client.cookies.set(session_cookie(secure=True), secret)
        yield Served(client=client, wiring=services, user=user)


def refusal(response: httpx.Response) -> tuple[int, dict[str, str], object]:
    """Everything a client can see of an answer: the status, the headers, the body."""
    return response.status_code, dict(response.headers), response.json()


def titles(response: httpx.Response) -> list[str]:
    return [item["title"] for item in response.json()["items"]]


# --- listing -----------------------------------------------------------------


@asyncio_test
async def test_the_panel_lists_this_persons_conversations_newest_updated_first() -> None:
    async with served() as it:
        for seconds in (0, 20, 10):
            await it.written(id=uuid.uuid4(), updated_at=at(seconds), title=f"at {seconds}")
        await it.wiring.store.add_conversation(
            conversation(id=uuid.uuid4(), owner_id=SOMEBODY_ELSE.id, title="not mine")
        )

        listed = await it.client.get("/api/conversations")

    assert listed.status_code == 200
    assert titles(listed) == ["at 20", "at 10", "at 0"]
    assert listed.json()["next_cursor"] is None


@asyncio_test
async def test_a_page_hands_back_the_cursor_the_next_one_is_asked_with() -> None:
    async with served() as it:
        for seconds in range(5):
            await it.written(id=uuid.uuid4(), updated_at=at(seconds), title=f"at {seconds}")

        first = await it.client.get("/api/conversations", params={"limit": 2})
        cursor = first.json()["next_cursor"]
        second = await it.client.get("/api/conversations", params={"limit": 2, "cursor": cursor})
        nothing = await it.client.get("/api/conversations", params={"limit": 2, "cursor": ""})

    assert titles(first) == ["at 4", "at 3"]
    assert isinstance(cursor, str)
    assert titles(second) == ["at 2", "at 1"]
    # An empty cursor is a cursor nobody sent: the first page, not a refusal.
    assert titles(nothing) == titles(first)


@asyncio_test
async def test_a_cursor_that_is_not_one_is_refused_without_repeating_it() -> None:
    """It came from a browser, so it is a value like any other."""
    made_up = "1970-01-01T00:00:00+00:00|not-a-uuid"

    async with served() as it:
        refused = await it.client.get("/api/conversations", params={"cursor": made_up})

    assert refused.status_code == 422
    assert refused.json()["error"] == "InvalidCursorError"
    # The field is the route's to name; the rule is what the store says.
    assert refused.json()["detail"] == "query.cursor: that is not a cursor this store wrote"
    assert made_up not in refused.text


@asyncio_test
async def test_a_query_parameter_given_twice_is_refused_rather_than_read_once() -> None:
    """One question, two answers: FastAPI takes the last and says nothing."""
    async with served() as it:
        await it.written()

        refused = await it.client.get(
            "/api/conversations", params=[("limit", "99999"), ("limit", "2")]
        )
        cursors = await it.client.get(
            "/api/conversations", params=[("cursor", "a"), ("cursor", "b")]
        )

    assert refused.status_code == cursors.status_code == 422
    assert refused.json() == {
        "error": "InvalidValueError",
        "detail": "query.limit: given more than once",
    }
    assert cursors.json()["detail"] == "query.cursor: given more than once"
    assert "99999" not in refused.text


@asyncio_test
async def test_a_cursor_from_somebody_elses_listing_reaches_none_of_theirs() -> None:
    """A cursor is a position and not a key: it can only move a window.

    One issued in another person's listing parses, so it is read as the
    position it is -- and what it pages is still the caller's own
    conversations (``docs/specs/conversations.md``).
    """
    async with served() as it:
        for seconds in (40, 30):
            await it.wiring.store.add_conversation(
                conversation(
                    id=uuid.uuid4(),
                    owner_id=SOMEBODY_ELSE.id,
                    updated_at=at(seconds),
                    title="not mine",
                )
            )
        mine = [
            await it.written(id=uuid.uuid4(), updated_at=at(seconds), title=f"at {seconds}")
            for seconds in (20, 10)
        ]
        elsewhere = await it.wiring.conversations.list_for(SOMEBODY_ELSE, limit=1)

        listed = await it.client.get("/api/conversations", params={"cursor": elsewhere.cursor})

    # The stranger's own listing had a next page, so there is a cursor to try:
    # a position of theirs, newer than anything of the caller's.
    assert isinstance(elsewhere.cursor, str)
    assert [item["id"] for item in listed.json()["items"]] == [str(kept.id) for kept in mine]


@asyncio_test
async def test_a_limit_outside_the_bound_is_refused_by_the_document() -> None:
    """The bound is in the OpenAPI document, so a client can hold itself to it."""
    async with served() as it:
        above = await it.client.get("/api/conversations", params={"limit": 4_000})
        below = await it.client.get("/api/conversations", params={"limit": 0})
        empty = await it.client.get("/api/conversations", params={"limit": ""})

    assert above.status_code == below.status_code == empty.status_code == 422
    assert above.json() == {
        "error": "InvalidValueError",
        "detail": f"query.limit: {UNREADABLE_RULES['less_than_equal']}",
    }
    assert below.json()["detail"] == f"query.limit: {UNREADABLE_RULES['greater_than_equal']}"
    # An empty one is not a whole number, and is told so rather than read as
    # the default: only a parameter that is not sent is not sent.
    assert empty.json()["detail"] == f"query.limit: {UNREADABLE_RULES['int_parsing']}"
    assert "4000" not in above.text


def test_every_query_parameter_is_given_once() -> None:
    """The listing's parameters and the ones ``given_once`` is asked about.

    Hand-listing what to check is how a third parameter added later goes
    unchecked, so the two are held together: what the route takes is what
    FastAPI solved it with, and ``PAGING`` is what the route asks about.
    """
    listing = next(
        route
        for route in api_routes(create_api().router)
        if route.path == "/api/conversations" and route.methods == {"GET"}
    )

    assert {parameter.name for parameter in listing.dependant.query_params} == set(PAGING)


def test_the_page_bound_is_the_one_the_application_enforces() -> None:
    """Two numbers in two places is one number that will drift.

    The document's bound and the application's refusal are the same
    ``application.MAX_PAGE``, which is the store's own (``ports.MAX_PAGE``):
    ``api`` may not import ``ports``, so the application is the door it comes
    through.
    """
    document = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    limit = next(
        parameter
        for parameter in document["paths"]["/api/conversations"]["get"]["parameters"]
        if parameter["name"] == "limit"
    )

    assert (limit["schema"]["minimum"], limit["schema"]["maximum"]) == (1, MAX_PAGE)
    assert limit["schema"]["default"] == DEFAULT_PAGE


# --- opening -----------------------------------------------------------------


@asyncio_test
async def test_opening_a_conversation_sends_the_thread_it_shows_and_nothing_put_aside() -> None:
    """After an edit: the path to the newest message, as a list, and no parents.

    The edited-away question and the answer under it stay in the store and
    are nowhere in the response.
    """
    asked = question("What is a robinaut?", seconds=1)
    said = answer(asked, ANSWER, seconds=2)
    followed = question("And why?", parent=said, seconds=3)
    replied = answer(followed, "Because.", seconds=4)
    edited = question("And why not?", parent=said, seconds=5)

    async with served() as it:
        kept = await it.written(asked, said, followed, replied, edited)

        opened = await it.client.get(f"/api/conversations/{kept.id}")
        stored = await it.wiring.store.messages_of(kept.id)

    body = opened.json()
    assert opened.status_code == 200
    assert body["conversation"]["id"] == str(kept.id)
    assert [message["id"] for message in body["messages"]] == [
        str(asked.id),
        str(said.id),
        str(edited.id),
    ]
    assert len(stored) == 5
    assert "parent_id" not in body["messages"][0]
    assert "leaf_id" not in body
    assert "active_leaf_id" not in body["conversation"]
    assert body["messages"][0]["parts"] == [{"kind": "text", "text": "What is a robinaut?"}]
    assert body["messages"][0]["role"] == Role.USER.value
    assert body["messages"][0]["provenance"] is None
    assert body["messages"][1]["provenance"] == {
        "agent": AGENT,
        "engine": "pydantic-ai",
        "model": "sonnet",
        "run_id": str(said.provenance.run_id),
    }
    assert body["messages"][1]["created_at"] == "2026-09-21T09:00:02Z"
    assert (body["run_id"], body["resume"], body["ended_badly"]) == (None, None, None)


@asyncio_test
async def test_after_a_regeneration_only_the_new_answer_is_sent() -> None:
    asked = question("What is a robinaut?", seconds=1)
    said = answer(asked, ANSWER, seconds=2)
    again = answer(asked, "Someone who plays fair, briefly.", seconds=3)

    async with served() as it:
        kept = await it.written(asked, said, again)

        opened = await it.client.get(f"/api/conversations/{kept.id}")
        stored = await it.wiring.store.messages_of(kept.id)

    assert [message["id"] for message in opened.json()["messages"]] == [
        str(asked.id),
        str(again.id),
    ]
    assert len(stored) == 3


@asyncio_test
async def test_editing_the_first_question_sends_only_the_new_roots_path() -> None:
    asked = question("What is a robinaut?", seconds=1)
    said = answer(asked, ANSWER, seconds=2)
    edited = question("What is a robinaut, briefly?", seconds=3)

    async with served() as it:
        kept = await it.written(asked, said, edited)

        opened = await it.client.get(f"/api/conversations/{kept.id}")

    assert [message["id"] for message in opened.json()["messages"]] == [str(edited.id)]


@asyncio_test
async def test_somebody_elses_conversation_is_not_read_before_it_is_refused() -> None:
    """ "Not yours" must not cost more than "not there", or a clock tells them apart.

    Opening reads every message of a conversation, the run in flight and each
    of that run's events, and decodes every document. Checking ownership after
    that read would make a stranger's conversation take as long as its own
    author's -- measurably longer than an id that is not there -- which is one
    of the ways an id is probed for existence. It would also let any signed-in
    caller make this deployment read somebody else's thousand messages.
    """
    counting = CountingStore()

    async with served(store=counting) as it:
        theirs = conversation(id=uuid.uuid4(), owner_id=SOMEBODY_ELSE.id, title="not mine")
        await counting.add_conversation(theirs)
        for seconds in range(1, 6):
            asked = question(f"question {seconds}", conversation_id=theirs.id, seconds=seconds)
            await counting.append_message(asked, message_to_data(asked), now=asked.created_at)

        refused = await it.client.get(f"/api/conversations/{theirs.id}")
        absent = await it.client.get(f"/api/conversations/{NOWHERE}")

    assert counting.snapshots == 0
    assert refusal(refused) == refusal(absent)
    assert refused.json() == {"error": NOT_FOUND_ERROR, "detail": NOT_FOUND_DETAIL}


@asyncio_test
async def test_opening_a_conversation_reads_one_moment_of_it() -> None:
    """And one moment only: the caller's own conversation costs one snapshot."""
    counting = CountingStore()

    async with served(store=counting) as it:
        kept = await it.written(question(seconds=1))

        opened = await it.client.get(f"/api/conversations/{kept.id}")

    assert opened.status_code == 200
    assert counting.snapshots == 1


@asyncio_test
async def test_an_empty_conversation_opens_on_nothing() -> None:
    async with served() as it:
        kept = await it.written()

        opened = await it.client.get(f"/api/conversations/{kept.id}")

    assert opened.json()["messages"] == []


@asyncio_test
async def test_a_turn_with_tools_is_served_as_data() -> None:
    """The answer's calls and the tool message's results are sent as parts of
    their own kind, with the arguments as an object and the error flag as a
    boolean, and never as text (``docs/specs/wire.md``)."""
    asked = question("look it up", seconds=1)
    calling = answer(
        asked,
        seconds=2,
        parts=(
            TextPart("Let me look."),
            ToolCallPart("toolu_01", "github__search", {"q": "robinauts", "n": 3}),
        ),
        extras={"anthropic": {"thinking": [{"signature": "sig"}]}},
    )
    results = Message(
        id=uuid.uuid4(),
        conversation_id=asked.conversation_id,
        parent_id=calling.id,
        role=Role.TOOL,
        parts=(ToolResultPart("toolu_01", "found <b>3</b>", is_error=True),),
        created_at=at(3),
    )
    final = answer(results, "Found three.", seconds=4)

    async with served() as it:
        kept = await it.written(asked, calling, results, final)

        opened = await it.client.get(f"/api/conversations/{kept.id}")

    body = opened.json()
    assert opened.status_code == 200
    assert [message["role"] for message in body["messages"]] == [
        "user",
        "assistant",
        "tool",
        "assistant",
    ]
    assert body["messages"][1]["parts"] == [
        {"kind": "text", "text": "Let me look."},
        {
            "kind": "tool_call",
            "call_id": "toolu_01",
            "name": "github__search",
            "arguments": {"q": "robinauts", "n": 3},
        },
    ]
    assert body["messages"][2]["parts"] == [
        {"kind": "tool_result", "call_id": "toolu_01", "text": "found <b>3</b>", "is_error": True}
    ]
    assert body["messages"][2]["provenance"] is None
    # The vendor's extras are the store's and the adapter's, never the browser's.
    assert "extras" not in body["messages"][1]
    assert "sig" not in opened.text


@asyncio_test
async def test_reasoning_stored_in_a_message_is_not_sent() -> None:
    """Nothing writes one today; the wire says so all the same.

    ``domain.kept_parts`` drops reasoning between the engine and the store, so
    no message this build writes holds any. A row that did -- a build that
    keeps it, or an operator rolled back past one -- must still not have it
    read as part of the answer by a client generated from this document
    (``docs/specs/conversations.md``).
    """
    asked = question(seconds=1)
    thought = answer(asked, seconds=2, parts=(TextPart(ANSWER), ReasoningPart("hmm, fairness")))

    async with served() as it:
        kept = await it.written(asked, thought)

        opened = await it.client.get(f"/api/conversations/{kept.id}")

    assert opened.json()["messages"][1]["parts"] == [{"kind": "text", "text": ANSWER}]
    assert "fairness" not in opened.text


BEGUN = 1
"""Where a run's stream stands once it has begun: ``RunStarted``, at 1."""

ANSWERED = 4
"""Where a run's stream stands once one answer is complete.

``RunStarted``, ``MessageStarted``, one ``TextPiece`` and
``MessageCompleted``, numbered from 1 with no gaps (``docs/specs/runs.md``).
"""


@asyncio_test
async def test_opening_a_conversation_with_a_run_in_flight_says_where_to_attach() -> None:
    """One answer stored, another about to arrive: attach after the first.

    The messages that are complete are in the tree, so the point to attach at
    is the position of the last one -- what follows it is the message still
    being produced, from its announcement, and nothing is shown twice
    (``docs/specs/runs.md``).
    """
    gate = Gate()

    async with served(*says(ANSWER), gate, *says("And that is all.")) as it:
        started = await it.wiring.turns.start(it.user, agent_id=AGENT, text="What is a robinaut?")
        submitted(it.wiring, started.run)
        await stream_reached(it.wiring.store, started.run.id, ANSWERED)

        opened = await it.client.get(f"/api/conversations/{started.conversation.id}")

        gate.open()
        await settled(it.wiring, started.run)

    body = opened.json()
    assert body["run_id"] == str(started.run.id)
    assert [message["parts"][0]["text"] for message in body["messages"]] == [
        "What is a robinaut?",
        ANSWER,
    ]
    assert body["resume"] == {"after": ANSWERED, "follows": str(body["messages"][-1]["id"])}
    assert body["ended_badly"] is None


@asyncio_test
async def test_a_run_that_has_completed_nothing_is_attached_to_at_its_beginning() -> None:
    """Nothing to skip: the whole answer is still to come.

    ``after`` is the position of the event that started the run, and
    ``follows`` is the question the run answers -- what the first message
    announced will hang under, which only the run knows (``core.resume_point``).
    """
    gate = Gate()

    async with served(gate, *says(ANSWER)) as it:
        started = await it.wiring.turns.start(it.user, agent_id=AGENT, text="What is a robinaut?")
        submitted(it.wiring, started.run)
        await stream_reached(it.wiring.store, started.run.id, BEGUN)

        opened = await it.client.get(f"/api/conversations/{started.conversation.id}")

        gate.open()
        await settled(it.wiring, started.run)

    assert started.message is not None
    assert opened.json()["run_id"] == str(started.run.id)
    assert opened.json()["resume"] == {"after": BEGUN, "follows": str(started.message.id)}
    assert [message["id"] for message in opened.json()["messages"]] == [str(started.message.id)]


@asyncio_test
async def test_opening_during_a_regeneration_sends_the_thread_up_to_its_question() -> None:
    """Regenerating the first answer of two turns: only the first question is sent.

    A regeneration stores nothing until its answer completes, so while it is
    going the newest message is still the last answer of the old thread. What
    is sent ends where the run is writing -- ``resume.follows`` -- so that the
    new answer arriving is shown in place of what it replaces, and not after
    it (``core.ConversationTree.visible_path``).
    """
    asked = question("What is a robinaut?", seconds=1)
    said = answer(asked, ANSWER, seconds=2)
    followed = question("And why?", parent=said, seconds=3)
    replied = answer(followed, "Because.", seconds=4)
    gate = Gate()
    steps = says("Someone fair.")
    steps.insert(-1, gate)

    async with served(*steps) as it:
        kept = await it.written(asked, said, followed, replied)
        started = await it.wiring.turns.regenerate(
            it.user, conversation_id=kept.id, message_id=said.id
        )
        submitted(it.wiring, started.run)
        await gate.reached.wait()

        during = await it.client.get(f"/api/conversations/{kept.id}")

        gate.open()
        await settled(it.wiring, started.run)
        after = await it.client.get(f"/api/conversations/{kept.id}")

    body = during.json()
    assert body["run_id"] == str(started.run.id)
    assert [message["id"] for message in body["messages"]] == [str(asked.id)]
    assert body["resume"]["follows"] == str(asked.id)
    assert [message["parts"][0]["text"] for message in after.json()["messages"]] == [
        "What is a robinaut?",
        "Someone fair.",
    ]


@asyncio_test
async def test_opening_a_conversation_after_a_run_went_wrong_says_so() -> None:
    """A reload after a failure is told, instead of finding a turn that stops."""
    async with served() as it:
        started = await it.wiring.turns.start(it.user, agent_id=AGENT, text="What is a robinaut?")
        cancelled = await it.wiring.turns.cancel(it.user, started.run.id)

        opened = await it.client.get(f"/api/conversations/{started.conversation.id}")

    body = opened.json()
    assert (body["run_id"], body["resume"]) == (None, None)
    assert body["ended_badly"] == {
        "run_id": str(started.run.id),
        "state": RunState.CANCELLED.value,
        "ended_at": "2026-09-21T09:01:40Z",
    }
    assert cancelled.state is RunState.CANCELLED


# --- renaming and moving -----------------------------------------------------


@asyncio_test
async def test_renaming_answers_the_conversation_the_store_wrote() -> None:
    async with served() as it:
        kept = await it.written()

        renamed = await it.client.patch(
            f"/api/conversations/{kept.id}", json={"title": NEW_TITLE}, headers=WRITE
        )

        stored = await it.stored(kept.id)

    assert renamed.status_code == 200
    assert renamed.json()["title"] == NEW_TITLE
    assert stored is not None
    assert stored.title == NEW_TITLE
    # The store dated it, and what came back is what it wrote.
    assert renamed.json()["updated_at"] == "2026-09-21T09:01:40Z"


@asyncio_test
async def test_a_title_no_conversation_could_hold_is_refused_naming_the_rule() -> None:
    async with served() as it:
        kept = await it.written()

        refused = await it.client.patch(
            f"/api/conversations/{kept.id}", json={"title": "secret\nsauce"}, headers=WRITE
        )

        stored = await it.stored(kept.id)

    assert refused.status_code == 422
    assert refused.json() == {
        "error": "InvalidValueError",
        "detail": "body.title: a conversation's title is one line of printable text",
    }
    assert "sauce" not in refused.text
    assert stored is not None
    assert stored.title == "What is a robinaut?"


@asyncio_test
async def test_a_body_carrying_fields_nobody_knows_is_refused_without_naming_them() -> None:
    """``extra="forbid"``, and **the key is the request's too**.

    A body arrives with whatever keys the sender likes -- a token pasted into
    the wrong tool is a key as readily as a value -- and pydantic reports each
    extra one with the key inside the location it refused. So they are
    counted, not named (``api.unknown_fields``).
    """
    token = "sk-live-do-not-echo-me"

    async with served() as it:
        kept = await it.written()

        one = await it.client.patch(
            f"/api/conversations/{kept.id}",
            json={"title": NEW_TITLE, token: "x"},
            headers=WRITE,
        )
        two = await it.client.patch(
            f"/api/conversations/{kept.id}",
            json={"title": NEW_TITLE, token: "x", "owner_id": str(NOWHERE)},
            headers=WRITE,
        )

        stored = await it.stored(kept.id)

    assert one.status_code == two.status_code == 422
    assert one.json()["detail"] == f"body: {UNKNOWN_FIELD}"
    assert two.json()["detail"] == "body: 2 fields nobody knows"
    assert token not in one.text
    assert token not in two.text
    assert "owner_id" not in two.text
    assert stored is not None
    assert stored.title == "What is a robinaut?"


@asyncio_test
async def test_an_id_that_is_no_uuid_is_refused_without_a_piece_of_it_coming_back() -> None:
    """Pydantic's own message says which character it tripped over.

    ``invalid character: found `Z` at 1`` -- which is a piece of what was sent,
    in a body a client is shown. Every kind of thing pydantic refuses has a
    sentence of **ours** instead (``api.UNREADABLE_RULES``).
    """
    async with served() as it:
        refused = await it.client.get("/api/conversations/aZbZcZ-not-a-uuid")

    assert refused.status_code == 422
    assert refused.json() == {
        "error": "InvalidValueError",
        "detail": f"path.conversation_id: {UNREADABLE_RULES['uuid_parsing']}",
    }
    assert "Z" not in refused.json()["detail"]
    assert "aZbZcZ" not in refused.text


@asyncio_test
async def test_a_title_longer_than_a_conversation_holds_is_refused_by_the_document() -> None:
    """The bound is in the OpenAPI document, so a client need not find out by asking."""
    async with served() as it:
        kept = await it.written()

        refused = await it.client.patch(
            f"/api/conversations/{kept.id}",
            json={"title": "sauce" * 40},
            headers=WRITE,
        )

    assert refused.status_code == 422
    assert refused.json()["detail"] == f"body.title: {UNREADABLE_RULES['string_too_long']}"
    assert "sauce" not in refused.text


@asyncio_test
async def test_a_title_that_is_not_text_at_all_is_refused_as_such() -> None:
    """A lone surrogate reads as JSON and is no string the platform can store.

    It is one of the two things stored text cannot hold
    (``docs/specs/conversations.md``); pydantic refuses it before the
    application sees it, and what is said is which field and which rule
    (``api.UNREADABLE_RULES``).
    """
    async with served() as it:
        kept = await it.written()

        refused = await it.client.patch(
            f"/api/conversations/{kept.id}", content=LONE_HALF, headers=WRITE
        )

        stored = await it.stored(kept.id)

    assert refused.status_code == 422
    assert refused.json() == {
        "error": "InvalidValueError",
        "detail": f"body.title: {UNREADABLE_RULES['string_unicode']}",
    }
    assert stored is not None
    assert stored.title == "What is a robinaut?"


@asyncio_test
async def test_a_title_of_nothing_but_spaces_is_no_title() -> None:
    """A conversation named three spaces has a name nobody can read.

    The record cannot refuse it -- a space is printable and on one line -- so
    the application does, the way an agent's title is refused
    (``domain.AgentDefinition``).
    """
    async with served() as it:
        kept = await it.written()

        blank = await it.client.patch(
            f"/api/conversations/{kept.id}", json={"title": "   "}, headers=WRITE
        )
        empty = await it.client.patch(
            f"/api/conversations/{kept.id}", json={"title": ""}, headers=WRITE
        )

        stored = await it.stored(kept.id)

    assert blank.status_code == empty.status_code == 422
    assert blank.json() == {
        "error": "InvalidValueError",
        "detail": "body.title: a conversation's title has something in it",
    }
    # The empty one never reaches the application: the document says a title
    # has a character in it.
    assert empty.json()["detail"] == f"body.title: {UNREADABLE_RULES['string_too_short']}"
    assert stored is not None
    assert stored.title == "What is a robinaut?"


@asyncio_test
async def test_a_write_sent_as_anything_but_json_is_refused() -> None:
    """The other refusal the request protection answers before a route runs."""
    async with served() as it:
        kept = await it.written()

        refused = await it.client.patch(
            f"/api/conversations/{kept.id}",
            content=b'{"title": "Robinauts"}',
            headers={"content-type": "text/plain", "origin": PUBLIC_URL},
        )

        stored = await it.stored(kept.id)

    assert refused.status_code == 415
    assert refused.json()["error"] == "UnsupportedMediaTypeError"
    assert stored is not None
    assert stored.title == "What is a robinaut?"


@asyncio_test
async def test_a_body_that_is_not_json_names_no_offset_of_its_own() -> None:
    """Pydantic's location for unreadable JSON is the character it stopped at.

    ``body.2012`` is a number computed from what was sent, in a body a client
    is shown, so a location keeps only the pieces that are **names**
    (``api.errors``).
    """
    async with served() as it:
        kept = await it.written()

        refused = await it.client.patch(
            f"/api/conversations/{kept.id}",
            content=b'{"title": "Robinauts"' + b" " * 2_000,
            headers=WRITE,
        )

    assert refused.status_code == 422
    assert refused.json() == {
        "error": "InvalidValueError",
        "detail": f"body: {UNREADABLE_RULES['json_invalid']}",
    }
    assert not any(character.isdigit() for character in refused.json()["detail"])


TOO_DEEP = 30_000
"""A nesting no parser here will finish: several times the interpreter's own
budget for recursing into one, so this is the same ``RecursionError`` whatever
a build's limit is and however deep the stack already was."""


@asyncio_test
async def test_a_body_nested_deeper_than_it_can_be_read_is_refused() -> None:
    """A body ``json`` runs out of stack on is refused, not let through.

    The check reads the body a **second** time, and further down the stack
    than the framework read it, so there is a band of nesting -- a few levels
    wide, and where it falls depends on the stack -- in which the framework's
    parse succeeds and this one raises. Unhandled that is a 500 with a
    traceback in the log for a request that is nobody's mistake but its
    sender's; and what it really is, is a body whose repeated fields nobody
    could check, which is not a body to accept.

    Asked here of a depth nothing could read, because the band itself moves
    with the stack: it is the same ``RecursionError``, from the same call.
    """
    deep = b"[" * TOO_DEEP + b"]" * TOO_DEEP
    request = Request(
        {"type": "http", "method": "PATCH", "path": "/", "headers": [], "query_string": b""},
        _sending(deep),
    )

    with errors_logged() as recorded:
        with pytest.raises(InvalidValueError) as raised:
            await read_once(request)

    assert str(raised.value) == BODY_TOO_DEEP
    # A refusal, not a bug of ours: nothing is logged, and the status is the
    # 422 every ``InvalidValueError`` answers with (``api.STATUS_OF``).
    assert recorded == []
    assert status_of(raised.value) == 422


def _sending(body: bytes) -> Callable[[], Awaitable[dict[str, object]]]:
    """A receive channel handing over ``body`` in one message, as a server does."""

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": body, "more_body": False}

    return receive


@contextmanager
def errors_logged() -> Iterator[list[logging.LogRecord]]:
    """Everything logged at ERROR or worse while this is open."""
    recorded: list[logging.LogRecord] = []

    class Kept(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            recorded.append(record)

    handler = Kept(level=logging.ERROR)
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        yield recorded
    finally:
        root.removeHandler(handler)


@asyncio_test
async def test_a_body_that_gives_a_field_twice_is_refused_at_any_depth() -> None:
    """``json.loads`` keeps the last of a repeated key and says nothing.

    So a write that asked two things would be answered on one of them, and a
    log, a proxy or a reviewer reading the same bytes may well pick the other
    (``api.read_once``).
    """
    async with served() as it:
        kept = await it.written()

        twice = await it.client.patch(
            f"/api/conversations/{kept.id}",
            content=b'{"title": "Safe", "title": "Evil"}',
            headers=WRITE,
        )
        deeper = await it.client.patch(
            f"/api/conversations/{kept.id}",
            content=b'{"title": {"a": 1, "a": 2}}',
            headers=WRITE,
        )

        stored = await it.stored(kept.id)

    assert twice.status_code == deeper.status_code == 422
    assert twice.json() == {"error": "InvalidValueError", "detail": BODY_TWICE}
    assert deeper.json()["detail"] == BODY_TWICE
    assert "Evil" not in twice.text
    assert stored is not None
    assert stored.title == "What is a robinaut?"


def test_every_route_with_a_body_reads_it_once() -> None:
    """The duplicate-key check is a dependency, so it can be left off a route.

    Nothing else in the request protection can be (it is middleware), so this
    is the one rule that needs a test to say it was not forgotten. A route
    that takes a body and does not declare ``read_once`` fails here.
    """
    forgotten = [
        f"{sorted(route.methods or ())} {route.path}"
        for route in api_routes(create_api().router)
        if route.body_field is not None
        and read_once not in [one.call for one in route.dependant.dependencies]
    ]

    assert forgotten == []
    # And there really are routes with a body, so this is not vacuous.
    assert [
        route.path for route in api_routes(create_api().router) if route.body_field is not None
    ] == [
        "/api/conversations/{conversation_id}",
        "/api/conversations/{conversation_id}/model",
        "/api/turns",
        "/api/conversations/{conversation_id}/turns",
    ]


@asyncio_test
async def test_a_path_with_a_slash_it_has_not_got_is_not_a_route() -> None:
    """No redirect: a generated client that followed one would change the call."""
    async with served() as it:
        asked = await it.client.get("/api/conversations/")

    assert asked.status_code == 404
    assert asked.json() == {"error": "NotFound", "detail": "not found"}


@asyncio_test
async def test_what_starlette_allows_is_kept_where_no_route_of_ours_matches() -> None:
    """``/openapi.json`` is not an ``APIRoute``, so its own ``Allow`` stands."""
    async with served() as it:
        refused = await it.client.request("DELETE", "/openapi.json", headers=WRITE)

    assert refused.status_code == 405
    assert sorted(refused.headers["allow"].split(", ")) == ["GET", "HEAD"]
    assert allowed_methods(create_api(), "/openapi.json") == ""


# --- deleting ----------------------------------------------------------------


@asyncio_test
async def test_deleting_takes_the_conversation_for_good() -> None:
    async with served() as it:
        kept = await it.written(question(seconds=1))

        deleted = await it.client.delete(f"/api/conversations/{kept.id}", headers=WRITE)
        opened = await it.client.get(f"/api/conversations/{kept.id}")
        again = await it.client.delete(f"/api/conversations/{kept.id}", headers=WRITE)

        stored = await it.stored(kept.id)

    assert (deleted.status_code, deleted.content) == (204, b"")
    assert stored is None
    # And it is gone in the one way everything gone is answered: deleting is
    # not idempotent, and a client that means "make sure it is gone" reads
    # both answers as success.
    assert opened.status_code == again.status_code == 404
    assert opened.json() == again.json() == {"error": NOT_FOUND_ERROR, "detail": NOT_FOUND_DETAIL}


@asyncio_test
async def test_a_conversation_that_is_still_answering_is_not_deleted() -> None:
    """409, and its author cancels the run first (``docs/specs/conversations.md``)."""
    async with served() as it:
        kept = await it.answering()

        refused = await it.client.delete(f"/api/conversations/{kept.id}", headers=WRITE)

        stored = await it.stored(kept.id)

    assert refused.status_code == 409
    assert refused.json()["error"] == "RunAlreadyActiveError"
    assert stored is not None


# --- cancelling --------------------------------------------------------------


@asyncio_test
async def test_cancelling_a_run_this_process_is_executing_stops_it() -> None:
    """The answer stops; the run comes back as it is, which is still ``running``.

    Asking is not the same as having happened: the work is cancelled here and
    the end is written a moment later, under a shield (``docs/specs/runs.md``).
    """
    gate = Gate()

    async with served(gate, *says(ANSWER)) as it:
        started = await it.wiring.turns.start(it.user, agent_id=AGENT, text="What is a robinaut?")
        submitted(it.wiring, started.run)
        await gate.reached.wait()

        stopped = await it.client.post(
            f"/api/conversations/{started.conversation.id}/runs/{started.run.id}/cancel",
            headers=WRITE,
        )

        await settled(it.wiring, started.run)
        ended = await it.wiring.store.run_by_id(started.run.id)

    assert stopped.status_code == 200
    assert stopped.json() == {
        "id": str(started.run.id),
        "state": RunState.RUNNING.value,
        "started_at": "2026-09-21T09:01:40Z",
        "ended_at": None,
    }
    assert ended is not None
    assert ended.state is RunState.CANCELLED


@asyncio_test
async def test_cancelling_a_run_no_process_holds_ends_it_in_the_store() -> None:
    """What a restart looks like: nothing is executing it, so the store decides."""
    async with served() as it:
        kept = await it.answering()
        active = await it.wiring.store.active_run_of(kept.id)
        assert active is not None

        stopped = await it.client.post(
            f"/api/conversations/{kept.id}/runs/{active.id}/cancel", headers=WRITE
        )

        ended = await it.wiring.store.run_by_id(active.id)

    assert stopped.status_code == 200
    assert stopped.json()["state"] == RunState.CANCELLED.value
    assert stopped.json()["ended_at"] == "2026-09-21T09:01:40Z"
    assert ended is not None
    assert ended.state is RunState.CANCELLED


@asyncio_test
async def test_a_run_of_another_conversation_is_not_this_conversations_run() -> None:
    """The URL names a pair, and a pair that is not one is nothing."""
    async with served() as it:
        mine = await it.answering()
        other = await it.answering(text="And what is fairness?")
        elsewhere = await it.wiring.store.active_run_of(other.id)
        assert elsewhere is not None

        crossed = await it.client.post(
            f"/api/conversations/{mine.id}/runs/{elsewhere.id}/cancel", headers=WRITE
        )
        absent = await it.client.post(
            f"/api/conversations/{mine.id}/runs/{NOWHERE}/cancel", headers=WRITE
        )

        still_going = await it.wiring.store.run_by_id(elsewhere.id)

    assert refusal(crossed) == refusal(absent)
    assert crossed.json() == {"error": NOT_FOUND_ERROR, "detail": NOT_FOUND_DETAIL}
    assert still_going is not None
    assert still_going.is_active


@asyncio_test
async def test_somebody_elses_run_is_answered_like_one_that_is_not_there() -> None:
    async with served() as it:
        theirs = await it.wiring.turns.start(SOMEBODY_ELSE, agent_id=AGENT, text="Mine, not yours")

        asked = await it.client.post(
            f"/api/conversations/{theirs.conversation.id}/runs/{theirs.run.id}/cancel",
            headers=WRITE,
        )
        absent = await it.client.post(
            f"/api/conversations/{NOWHERE}/runs/{NOWHERE}/cancel", headers=WRITE
        )

        still_going = await it.wiring.store.run_by_id(theirs.run.id)

    assert refusal(asked) == refusal(absent)
    assert asked.json() == {"error": NOT_FOUND_ERROR, "detail": NOT_FOUND_DETAIL}
    assert still_going is not None
    assert still_going.is_active


# --- the agents --------------------------------------------------------------


@asyncio_test
async def test_the_agents_are_offered_without_what_the_operator_wrote() -> None:
    async with served() as it:
        offered = await it.client.get("/api/agents")

    assert offered.status_code == 200
    assert offered.json() == {
        "items": [{"id": AGENT, "title": "Assistant", "engine": "pydantic-ai", "model": MODEL}]
    }
    # The system prompt is the operator's, and it is not a message.
    assert it.wiring.definition.system_prompt not in offered.text
    assert "prompt" not in offered.text


# --- the models --------------------------------------------------------------


@asyncio_test
async def test_the_models_are_offered_by_title_in_the_order_they_were_configured() -> None:
    """The id to pick by and the name to show, and nothing of the operator's.

    Configuration order, which is the operator's: ``OTHER_MODEL`` first here
    although it sorts after ``MODEL`` by id and by title. One with no title
    of its own is called by its id.
    """
    models = {
        OTHER_MODEL: model_config(OTHER_MODEL, name="vendor-opus-9", title="Claude Opus"),
        MODEL: model_config(MODEL, name="vendor-sonnet-9"),
    }

    async with served(models=models) as it:
        offered = await it.client.get("/api/models")

    assert offered.status_code == 200
    assert offered.json() == {
        "items": [
            {"id": OTHER_MODEL, "title": "Claude Opus"},
            {"id": MODEL, "title": MODEL},
        ]
    }
    # Which provider a model is reached through and what the vendor calls it
    # are the operator's.
    assert "vendor-" not in offered.text
    assert "anthropic" not in offered.text


@asyncio_test
async def test_moving_to_another_model_answers_the_conversation_the_store_wrote() -> None:
    async with served() as it:
        kept = await it.written()

        moved = await it.client.put(
            f"/api/conversations/{kept.id}/model", json={"model_id": OTHER_MODEL}, headers=WRITE
        )
        listed = await it.client.get("/api/conversations")
        opened = await it.client.get(f"/api/conversations/{kept.id}")

        stored = await it.stored(kept.id)

    assert kept.model == MODEL
    assert moved.status_code == 200
    assert moved.json()["model"] == OTHER_MODEL
    assert stored is not None
    assert stored.model == OTHER_MODEL
    # The store dated it, as a rename is, and what came back is what it wrote.
    assert moved.json()["updated_at"] == "2026-09-21T09:01:40Z"
    # And every other answer that carries the conversation says so too.
    assert listed.json()["items"][0]["model"] == OTHER_MODEL
    assert opened.json()["conversation"]["model"] == OTHER_MODEL


@asyncio_test
async def test_a_model_the_deployment_does_not_offer_is_refused_by_name() -> None:
    """422 under its own name, naming the field and never the id; the 404 is the conversation's."""
    async with served() as it:
        kept = await it.written()

        refused = await it.client.put(
            f"/api/conversations/{kept.id}/model", json={"model_id": "haiku"}, headers=WRITE
        )

        stored = await it.stored(kept.id)

    assert refused.status_code == 422
    assert refused.json() == {"error": "UnknownModelError", "detail": NOT_OFFERED_DETAIL}
    assert "haiku" not in refused.text
    assert stored == kept


@asyncio_test
async def test_a_model_not_offered_is_refused_before_the_conversation_is_looked_for() -> None:
    """422 whatever the conversation: somebody else's and one that never existed alike.

    The answer does not depend on the conversation, so it says nothing about
    it -- and the stranger's conversation is not touched.
    """
    async with served() as it:
        theirs = conversation(id=uuid.uuid4(), owner_id=SOMEBODY_ELSE.id, title="not mine")
        await it.wiring.store.add_conversation(theirs)

        asked = await it.client.put(
            f"/api/conversations/{theirs.id}/model", json={"model_id": "haiku"}, headers=WRITE
        )
        absent = await it.client.put(
            f"/api/conversations/{NOWHERE}/model", json={"model_id": "haiku"}, headers=WRITE
        )

        stored = await it.stored(theirs.id)

    assert refusal(asked) == refusal(absent)
    assert asked.status_code == 422
    assert asked.json() == {"error": "UnknownModelError", "detail": NOT_OFFERED_DETAIL}
    assert stored == theirs


@asyncio_test
async def test_a_model_id_no_model_could_have_is_refused_naming_the_rule() -> None:
    """The shape of a configured id is the application's rule, and it says so.

    The document holds only the length; the rest -- lower case, digits,
    ``-`` and ``_`` -- is ``domain.checked_config_id``'s, whose sentence
    describes what it refused without repeating it.
    """
    async with served() as it:
        kept = await it.written()

        refused = await it.client.put(
            f"/api/conversations/{kept.id}/model", json={"model_id": "Secret Sauce"}, headers=WRITE
        )

        stored = await it.stored(kept.id)

    assert refused.status_code == 422
    assert refused.json()["error"] == "InvalidValueError"
    assert refused.json()["detail"].startswith("body.model_id: a model's id is a name")
    assert "Sauce" not in refused.text
    assert stored == kept


@pytest.mark.parametrize(
    ("body", "detail"),
    [
        ({}, f"body.model_id: {UNREADABLE_RULES['missing']}"),
        ({"model_id": None}, f"body.model_id: {UNREADABLE_RULES['string_type']}"),
        ({"model_id": ""}, f"body.model_id: {UNREADABLE_RULES['string_too_short']}"),
        ({"model_id": "m" * 41}, f"body.model_id: {UNREADABLE_RULES['string_too_long']}"),
        ({"model_id": OTHER_MODEL, "model": MODEL}, f"body: {UNKNOWN_FIELD}"),
    ],
)
@asyncio_test
async def test_a_body_that_is_no_model_is_refused_by_the_document(
    body: dict[str, object], detail: str
) -> None:
    async with served() as it:
        kept = await it.written()

        refused = await it.client.put(
            f"/api/conversations/{kept.id}/model", json=body, headers=WRITE
        )

        stored = await it.stored(kept.id)

    assert refused.status_code == 422
    assert refused.json() == {"error": "InvalidValueError", "detail": detail}
    assert stored == kept


@asyncio_test
async def test_a_conversation_still_answering_is_moved_and_its_run_keeps_its_model() -> None:
    """Not refused like a delete: the change is what the **next** turn runs on."""
    async with served() as it:
        kept = await it.answering()

        moved = await it.client.put(
            f"/api/conversations/{kept.id}/model", json={"model_id": OTHER_MODEL}, headers=WRITE
        )

        snapshot = await it.wiring.store.conversation_snapshot(kept.id)

    assert moved.status_code == 200
    assert moved.json()["model"] == OTHER_MODEL
    assert snapshot.active_run is not None
    assert snapshot.active_run.model == MODEL


# --- ownership, and everything else about a route ----------------------------


def paths(conversation_id: uuid.UUID) -> Iterable[tuple[str, str, dict[str, object] | None]]:
    """Every route that names a conversation, against that id."""
    where = f"/api/conversations/{conversation_id}"
    yield "GET", where, None
    for method, rest, body in WRITES:
        yield method, f"{where}{rest}", body


@asyncio_test
async def test_everything_that_is_not_there_is_answered_in_one_way() -> None:
    """A conversation of somebody else's, and one that never existed.

    The same status, the same body and the same headers, on every route: the
    difference between "no such id" and "not yours" is the whole of what an
    attacker wants from an id (``docs/specs/conversations.md``).
    """
    async with served() as it:
        theirs = conversation(id=uuid.uuid4(), owner_id=SOMEBODY_ELSE.id, title="not mine")
        await it.wiring.store.add_conversation(theirs)

        for (method, mine, body), (_, nobodys, _) in zip(
            paths(theirs.id), paths(NOWHERE), strict=True
        ):
            asked = await it.client.request(method, mine, json=body, headers=WRITE)
            absent = await it.client.request(method, nobodys, json=body, headers=WRITE)

            assert refusal(asked) == refusal(absent), method
            assert asked.status_code == 404, method
            assert asked.json() == {"error": NOT_FOUND_ERROR, "detail": NOT_FOUND_DETAIL}

        # And it is still theirs afterwards.
        assert await it.stored(theirs.id) == theirs


@asyncio_test
async def test_no_route_here_is_reached_without_a_session() -> None:
    async with served() as it:
        kept = await it.written()
        it.client.cookies.clear()

        for method, where, body in [
            ("GET", "/api/conversations", None),
            ("GET", "/api/agents", None),
            ("GET", "/api/models", None),
            *paths(kept.id),
        ]:
            refused = await it.client.request(method, where, json=body, headers=WRITE)

            assert refused.status_code == 401, where
            assert refused.json()["error"] == "AuthenticationError", where

        assert await it.stored(kept.id) == kept


@asyncio_test
async def test_a_write_from_another_site_never_reaches_the_service() -> None:
    """The request protection, in front of every write here.

    It runs as middleware, so the refusal happens before a body is read and
    before a route is solved (``robinauts.legacy.api.protection``).
    """
    async with served() as it:
        # Nothing is answering in it, so every one of these writes would
        # **reach the service** if the protection let it through -- two of
        # them to be refused there, by a run id and a message id that are not
        # this conversation's. What answers here is the protection, in front
        # of every one.
        kept = await it.written(question(seconds=1))
        before = await it.stored(kept.id)

        for method, rest, body in WRITES:
            refused = await it.client.request(
                method, f"/api/conversations/{kept.id}{rest}", json=body, headers=CROSS_SITE
            )

            assert refused.status_code == 403, method
            assert refused.json()["error"] == "CrossSiteRequestError", method

        assert await it.stored(kept.id) == before


@asyncio_test
async def test_a_method_that_is_not_served_names_the_ones_that_are() -> None:
    """Three routes share this path, and ``Allow`` names all three.

    Starlette answers a 405 out of the first route whose path matched, so its
    own header would name one of them and a client would read that renaming a
    conversation is not allowed (``api.allowed_methods``).
    """
    async with served() as it:
        kept = await it.written()

        refused = await it.client.post(f"/api/conversations/{kept.id}", json={}, headers=WRITE)

    assert refused.status_code == 405
    assert refused.headers["allow"] == "DELETE, GET, PATCH"
    assert refused.json() == {"error": "MethodNotAllowed", "detail": "method not allowed"}


def test_the_wire_declares_exactly_the_values_it_sends() -> None:
    """An enum on the wire is a promise, so it names what is really sent.

    ``domain``'s own enums name more: every kind of content the stored format
    reserves a discriminator for -- reasoning among the carried ones, which is
    never sent -- and the states a run can be in that are not a bad end. A document
    that offered those would have every generated client branch on values it
    can never be sent. A value added here later is a change to the committed
    snapshot, which is what the snapshot is for.
    """
    assert set(get_args(SentKind)) == {kind.value for kind in SUPPORTED_PART_KINDS} - {
        PartKind.REASONING.value
    }
    assert set(get_args(SentRole)) == {role.value for role in SUPPORTED_ROLES}
    assert set(get_args(SentBadEnd)) == {state.value for state in FAULTED_RUN_STATES}


@asyncio_test
async def test_a_route_asked_for_before_start_up_says_nothing_about_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A deployment always has these services, so having none is a bug of ours.

    Not a 503 and not an empty list: the lifespan has not run, which is an
    application being served before it was started. It answers like every
    other mistake of ours -- the generic body -- and the whole of it goes to
    the log (``robinauts.legacy.api.access.NOT_WIRED``).
    """
    deployment = wired()
    secret, _ = await open_session(deployment)

    with caplog.at_level("ERROR"):
        async with serving(deployment.sign_in) as client:
            client.cookies.set(session_cookie(secure=True), secret)

            listed = await client.get("/api/conversations")
            offered = await client.get("/api/agents")
            models = await client.get("/api/models")

    assert listed.status_code == offered.status_code == models.status_code == 500
    assert listed.json() == {"error": INTERNAL_ERROR, "detail": GENERIC_DETAIL}
    assert NOT_WIRED in caplog.text
    assert "app.state.conversations" in caplog.text
    assert "app.state.turns" in caplog.text
