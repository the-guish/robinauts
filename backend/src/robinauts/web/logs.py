# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""How this process logs, and request values made safe to put in a log line.

A value from a request may carry a line break or another control character, which would let
it forge a log line of its own (CWE-117). Every such value goes through ``loggable`` on its way
to the log, validated or not, so that no log line depends on a check made elsewhere.

Every line says which process wrote it (``pod``, its worker id) and, where it applies, which
conversation and which turn it is about (``controller.contract.logs``): one line of text by
default, or one JSON object (``ROBINAUTS_LOG_FORMAT=json``), for a log collector of a fleet.
The access log has every query string cut off: one of this platform's paths carries an
authorization code in one.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import UTC, datetime
from typing import Any

from robinauts.controller.contract import logs

FORMAT_VARIABLE = "ROBINAUTS_LOG_FORMAT"
FORMATS = ("text", "json")

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_LONGEST = 200


def loggable(value: str) -> str:
    """``value`` with its control characters replaced by ``?`` and cut to 200 characters."""
    return _CONTROL.sub("?", value[:_LONGEST])


class PodContext(logging.Filter):
    """Puts the process's worker id, and the conversation and turn bound where the line was
    written, on every record."""

    def __init__(self, pod: str) -> None:
        super().__init__()
        self.pod = pod

    def filter(self, record: logging.LogRecord) -> bool:
        record.pod = self.pod
        record.session_id = logs.session_id.get()
        record.turn_id = logs.turn_id.get()
        return True


class TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        about = "".join(
            f" {name}={value}"
            for name, value in (
                ("conversation", getattr(record, "session_id", None)),
                ("turn", getattr(record, "turn_id", None)),
            )
            if value is not None
        )
        line = (
            f"{self.formatTime(record)} {record.levelname} {record.name}"
            f" pod={getattr(record, 'pod', '-')}{about} {record.getMessage()}"
        )
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        line: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "pod": getattr(record, "pod", None),
        }
        for name in ("session_id", "turn_id"):
            value = getattr(record, name, None)
            if value is not None:
                line[name] = value
        if record.exc_info:
            line["exc"] = self.formatException(record.exc_info)
        return json.dumps(line, ensure_ascii=False)


class NoQueryStrings(logging.Filter):
    """uvicorn's access log, with the query string of each path cut off."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
            record.args = (*args[:2], args[2].split("?", 1)[0], *args[3:])
        return True


def configure(pod: str, form: str = "text", level: int = logging.INFO) -> None:
    """One handler, on stdout, for every logger of the process, uvicorn's included."""
    if form not in FORMATS:
        raise ValueError(f"{FORMAT_VARIABLE} is {form!r}, not one of {', '.join(FORMATS)}")
    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(PodContext(pod))
    handler.setFormatter(JsonFormatter() if form == "json" else TextFormatter())
    root = logging.getLogger()
    for old in list(root.handlers):
        root.removeHandler(old)
    root.addHandler(handler)
    root.setLevel(level)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        named = logging.getLogger(name)
        named.handlers.clear()
        named.propagate = True
    logging.getLogger("uvicorn.access").addFilter(NoQueryStrings())
