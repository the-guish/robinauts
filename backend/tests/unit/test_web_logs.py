# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

from __future__ import annotations

import json
import logging
import uuid

import pytest

from robinauts.controller.contract import logs
from robinauts.web.logs import (
    JsonFormatter,
    NoQueryStrings,
    PodContext,
    TextFormatter,
    configure,
    loggable,
)


def test_a_plain_value_is_kept() -> None:
    assert loggable("okta") == "okta"


def test_a_line_break_cannot_forge_a_line() -> None:
    assert loggable("okta\r\nINFO user 1 signed in") == "okta??INFO user 1 signed in"


def test_other_control_characters_are_replaced() -> None:
    assert loggable("a\x00b\x1b[31mc\x7f") == "a?b?[31mc?"


def test_a_long_value_is_cut() -> None:
    assert loggable("x" * 500) == "x" * 200


def record(message: str, *args: object, name: str = "robinauts.test") -> logging.LogRecord:
    made = logging.LogRecord(name, logging.INFO, __file__, 1, message, args, None)
    PodContext("pod-a").filter(made)
    return made


def test_a_line_names_its_pod_and_what_it_is_about() -> None:
    session, turn = uuid.uuid4(), uuid.uuid4()
    assert TextFormatter().format(record("plain")).endswith(" pod=pod-a plain")
    with logs.about(session=session, turn=turn):
        said = TextFormatter().format(record("turn %s", "running"))
    assert f" pod=pod-a conversation={session} turn={turn} turn running" in said
    # Bound only where it was bound.
    assert "conversation=" not in TextFormatter().format(record("after"))


def test_a_json_line_is_one_object_with_the_same_ids() -> None:
    session, turn = uuid.uuid4(), uuid.uuid4()
    with logs.about(session=session, turn=turn):
        line = json.loads(JsonFormatter().format(record("turn %s", "failed")))
    assert line["msg"] == "turn failed"
    assert (line["pod"], line["session_id"], line["turn_id"]) == ("pod-a", str(session), str(turn))
    assert line["level"] == "INFO"
    assert line["ts"].endswith("+00:00")


def test_the_access_log_cuts_every_query_string_off() -> None:
    access = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("10.0.0.1:5", "GET", "/auth/callback/okta?code=secret&state=x", "1.1", 302),
        None,
    )
    NoQueryStrings().filter(access)
    assert access.getMessage() == '10.0.0.1:5 - "GET /auth/callback/okta HTTP/1.1" 302'


def test_an_unknown_format_is_refused() -> None:
    with pytest.raises(ValueError, match="ROBINAUTS_LOG_FORMAT"):
        configure("pod-a", "xml")
