# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""A turn whose runner went away: it ends as interrupted, its answer rebuilt from its events."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from typing import Any

from test_controller_turns import opened, settled

from aio import asyncio_test
from robinauts.agent_engines.contract.domain import Event, TextDelta, ToolCall, ToolResult
from robinauts.agent_engines.echo_engine.engine import EchoEngine
from robinauts.controller.contract.domain import (
    ArgumentsPiece,
    CallCompleted,
    CallStarted,
    Identity,
    MessageStarted,
    ReasoningPiece,
    ResultLanded,
    TextPart,
    TextPiece,
    ToolCallPart,
    ToolResultPart,
    TurnState,
)
from robinauts.controller.core.transcript import Partial, partial_answer

ANSWER = uuid.uuid4()
QUESTION = uuid.uuid4()


def test_the_parts_are_rebuilt_as_the_runner_keeps_them() -> None:
    events = [
        MessageStarted(ANSWER, QUESTION),
        ReasoningPiece(ANSWER, "hmm"),
        TextPiece(ANSWER, "Let me "),
        TextPiece(ANSWER, "look. "),
        CallStarted(ANSWER, "c1", "echo"),
        ArgumentsPiece(ANSWER, "c1", '{"text": '),
        ArgumentsPiece(ANSWER, "c1", '"hi"}'),
        CallCompleted(ANSWER, "c1"),
        ResultLanded(ANSWER, "c1", "hi"),
        TextPiece(ANSWER, "Now "),
        CallStarted(ANSWER, "c2", "search"),
        ArgumentsPiece(ANSWER, "c2", '{"q": "unfinish'),
    ]
    assert partial_answer(events) == Partial(
        ANSWER,
        QUESTION,
        (
            TextPart("Let me look. "),
            ToolCallPart("c1", "echo", {"text": "hi"}),
            ToolResultPart("c1", "hi", False),
            TextPart("Now "),
            ToolCallPart("c2", "search", {}),
        ),
    )


def test_no_answer_was_started_without_its_first_event() -> None:
    assert partial_answer([]) is None


class DyingEngine(EchoEngine):
    """Says something, calls its tool, then never says anything again: its process died."""

    def __init__(self) -> None:
        super().__init__()
        self.dies = True

    async def stream(self, *args: Any, **kwargs: Any) -> AsyncGenerator[Event, None]:
        if not self.dies:
            async for event in super().stream(*args, **kwargs):
                yield event
            return
        yield TextDelta("Looking. ")
        yield ToolCall(call_id="c1", name="echo", arguments={"text": "hi"})
        yield ToolResult(call_id="c1", name="echo", output="hi")
        await asyncio.Event().wait()


def jumped(by: timedelta) -> Any:
    return lambda: datetime.now(UTC) + by


@asyncio_test
async def test_an_expired_turn_ends_interrupted_with_what_it_did_and_can_be_retried() -> None:
    controller = await opened()
    engine = DyingEngine()
    controller._engines["echo"] = engine
    user = await controller.ensure_user(Identity("local", "me"))
    started = await controller.start_session(user, agent="echo", model="echo", text="hi")
    sid = started.session_id
    store = controller._store
    while len(await store.events_after(user.id, sid, started.turn_id, 0)) < 6:
        await store.wait_for_events(user.id, sid, started.turn_id, 0, 0.05)
    # The process died: its heartbeat stopped, and the lease passed.
    task, _ = controller._dispatcher._tasks[started.turn_id]
    task.cancel("lost")
    controller._now = jumped(timedelta(hours=1))

    shown = await controller.open_session(user, sid)
    assert shown.active is None
    assert shown.ended_badly is not None
    assert shown.ended_badly.state is TurnState.INTERRUPTED
    question, answer = shown.messages
    assert answer.failed
    assert answer.parent_id == question.id
    assert answer.turn_id == started.turn_id
    assert answer.parts == (
        TextPart("Looking. "),
        ToolCallPart("c1", "echo", {"text": "hi"}),
        ToolResultPart("c1", "hi", False),
    )
    engine.dies = False
    controller._now = lambda: datetime.now(UTC)
    again = await controller.retry_answer(user, sid, answer_id=answer.id, model="echo")
    await settled(controller, user, again)
    retried = await store.get_turn(user.id, sid, again.turn_id)
    assert retried is not None
    assert retried.state is TurnState.FINISHED
    assert retried.retries == answer.id
    await controller.close()


@asyncio_test
async def test_a_turn_the_deployment_stopped_keeps_what_it_did_as_a_failed_answer() -> None:
    controller = await opened()
    controller._close_timeout = 0.05
    controller._engines["echo"] = DyingEngine()
    user = await controller.ensure_user(Identity("local", "me"))
    started = await controller.start_session(user, agent="echo", model="echo", text="hi")
    sid = started.session_id
    store = controller._store
    while len(await store.events_after(user.id, sid, started.turn_id, 0)) < 6:
        await store.wait_for_events(user.id, sid, started.turn_id, 0, 0.05)
    await controller.close()
    turn = await store.get_turn(user.id, sid, started.turn_id)
    assert turn is not None
    assert turn.state is TurnState.INTERRUPTED
    shown = await controller.open_session(user, sid)
    answer = shown.messages[-1]
    assert answer.failed
    assert answer.parts[0] == TextPart("Looking. ")
    assert len(answer.parts) == 3
