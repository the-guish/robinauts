# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from typing import Any

import pytest
from test_controller_turns import opened, settled

from aio import asyncio_test
from robinauts.agent_engines.contract.domain import Event
from robinauts.agent_engines.echo_engine.engine import EchoEngine
from robinauts.controller.contract.context import WORK
from robinauts.controller.contract.domain import Identity
from robinauts.web.logs import FORMAT, Context, loggable


def test_a_plain_value_is_kept() -> None:
    assert loggable("okta") == "okta"


def test_a_line_break_cannot_forge_a_line() -> None:
    assert loggable("okta\r\nINFO user 1 signed in") == "okta??INFO user 1 signed in"


def test_other_control_characters_are_replaced() -> None:
    assert loggable("a\x00b\x1b[31mc\x7f") == "a?b?[31mc?"


def test_a_long_value_is_cut() -> None:
    assert loggable("x" * 500) == "x" * 200


def test_a_line_says_the_process_and_the_turn_it_is_about(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ROBINAUTS_WORKER_ID", "pod-a")
    record = logging.LogRecord("x", logging.INFO, "f", 1, "said", None, None)
    context = Context()
    assert context.filter(record)
    assert logging.Formatter(FORMAT).format(record) == "INFO:x:[pod=pod-a] said"
    token = WORK.set("conversation=c turn=t")
    try:
        context.filter(record)
    finally:
        WORK.reset(token)
    assert record.work == "pod=pod-a conversation=c turn=t"
    assert WORK.get() == ""


@asyncio_test
async def test_a_turn_runs_with_its_ids_in_the_log_context() -> None:
    seen: list[str] = []

    class Telling(EchoEngine):
        async def stream(self, *args: Any, **kwargs: Any) -> AsyncGenerator[Event, None]:
            seen.append(WORK.get())
            async for event in super().stream(*args, **kwargs):
                yield event

    controller = await opened()
    controller._engines["echo"] = Telling()
    user = await controller.ensure_user(Identity("local", "me"))
    started = await controller.start_session(user, agent="echo", model="echo", text="hi")
    await settled(controller, user, started)
    assert seen == [f"conversation={started.session_id} turn={started.turn_id}"]
    assert WORK.get() == ""
    await controller.close()
