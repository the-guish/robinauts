# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

from __future__ import annotations

import json
import logging

from test_controller_turns import opened, settled

from aio import asyncio_test
from robinauts.controller.contract.context import LOG_CONTEXT, LogContext
from robinauts.controller.contract.domain import Identity
from robinauts.web.logs import TEXT, Context, JsonLines, loggable


def test_a_plain_value_is_kept() -> None:
    assert loggable("okta") == "okta"


def test_a_line_break_cannot_forge_a_line() -> None:
    assert loggable("okta\r\nINFO user 1 signed in") == "okta??INFO user 1 signed in"


def test_other_control_characters_are_replaced() -> None:
    assert loggable("a\x00b\x1b[31mc\x7f") == "a?b?[31mc?"


def test_a_long_value_is_cut() -> None:
    assert loggable("x" * 500) == "x" * 200


class Kept(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []
        self.addFilter(Context("pod-a"))
        self.setFormatter(JsonLines())

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))


@asyncio_test
async def test_a_turns_lines_name_the_pod_the_conversation_and_the_turn() -> None:
    kept = Kept()
    root = logging.getLogger()
    root.addHandler(kept)
    level = root.level
    root.setLevel(logging.INFO)
    try:
        controller = await opened()
        user = await controller.ensure_user(Identity("local", "me"))
        started = await controller.start_session(user, agent="echo", model="echo", text="hi")
        await settled(controller, user, started)
        logging.getLogger("elsewhere").info("not about a turn")
        await controller.close()
    finally:
        root.removeHandler(kept)
        root.setLevel(level)
    lines = [json.loads(line) for line in kept.lines]
    about = [line for line in lines if line.get("turn") == str(started.turn_id)]
    assert {line["msg"] for line in about} >= {"turn started", "turn ended as finished"}
    assert all(line["conversation"] == str(started.session_id) for line in about)
    assert all(line["pod"] == "pod-a" and line["attempt"] == 1 for line in about)
    elsewhere = next(line for line in lines if line["msg"] == "not about a turn")
    assert "turn" not in elsewhere


def test_a_text_line_says_the_same() -> None:
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "said", None, None)
    token = LOG_CONTEXT.set(LogContext("c-1", "t-1", 2))
    try:
        Context("pod-a").filter(record)
    finally:
        LOG_CONTEXT.reset(token)
    line = logging.Formatter(TEXT).format(record)
    assert line.endswith("x pod=pod-a conversation=c-1 turn=t-1 attempt=2 said")
