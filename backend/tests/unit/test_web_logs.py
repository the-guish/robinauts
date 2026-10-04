# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

from __future__ import annotations

import contextvars
import json
import logging
import uuid
from collections.abc import Iterator

import pytest

from robinauts.controller.contract import context
from robinauts.web.logs import configure, loggable

SESSION = uuid.uuid4()
TURN = uuid.uuid4()


def test_a_plain_value_is_kept() -> None:
    assert loggable("okta") == "okta"


def test_a_line_break_cannot_forge_a_line() -> None:
    assert loggable("okta\r\nINFO user 1 signed in") == "okta??INFO user 1 signed in"


def test_other_control_characters_are_replaced() -> None:
    assert loggable("a\x00b\x1b[31mc\x7f") == "a?b?[31mc?"


def test_a_long_value_is_cut() -> None:
    assert loggable("x" * 500) == "x" * 200


@pytest.fixture
def root_restored() -> Iterator[None]:
    loggers = [logging.getLogger(n) for n in ("", "uvicorn", "uvicorn.error", "uvicorn.access")]
    kept = [(each.handlers[:], each.level, each.propagate, each.disabled) for each in loggers]
    try:
        yield
    finally:
        for each, (handlers, level, propagate, disabled) in zip(loggers, kept, strict=True):
            each.handlers, each.propagate, each.disabled = handlers, propagate, disabled
            each.setLevel(level)


def test_a_line_names_the_process_and_the_turn_it_is_about(
    root_restored: None, capsys: pytest.CaptureFixture[str]
) -> None:
    configure("text", "pod-a")
    logging.getLogger("robinauts.test").info("before")

    def in_a_turn() -> None:
        context.about(SESSION, TURN, 2)
        logging.getLogger("robinauts.test").info("during")

    contextvars.copy_context().run(in_a_turn)
    before, during = capsys.readouterr().out.splitlines()
    assert "[pod=pod-a] before" in before
    assert f"[pod=pod-a conversation={SESSION} turn={TURN} attempt=2] during" in during


def test_json_lines_carry_the_same(root_restored: None, capsys: pytest.CaptureFixture[str]) -> None:
    configure("json", "pod-b")

    def in_a_turn() -> None:
        context.about(SESSION, TURN, 1)
        logging.getLogger("robinauts.test").warning("said %s", "this")

    contextvars.copy_context().run(in_a_turn)
    line = json.loads(capsys.readouterr().out)
    assert {k: line[k] for k in ("level", "logger", "msg", "pod", "session_id", "turn_id")} == {
        "level": "WARNING",
        "logger": "robinauts.test",
        "msg": "said this",
        "pod": "pod-b",
        "session_id": str(SESSION),
        "turn_id": str(TURN),
    }
    assert line["attempt"] == 1


def test_the_access_log_keeps_the_path_and_drops_the_query(
    root_restored: None, capsys: pytest.CaptureFixture[str]
) -> None:
    configure("text", "pod-a")
    logging.getLogger("uvicorn.access").info(
        '%s - "%s %s HTTP/%s" %d',
        "10.0.0.1:5",
        "GET",
        "/auth/callback/okta?code=s3cret",
        "1.1",
        302,
    )
    said = capsys.readouterr().out
    assert "/auth/callback/okta" in said
    assert "s3cret" not in said


def test_an_unknown_format_is_refused(root_restored: None) -> None:
    with pytest.raises(ValueError, match="ROBINAUTS_LOG_FORMAT"):
        configure("yaml", "pod-a")
