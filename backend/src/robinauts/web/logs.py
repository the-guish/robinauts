# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The log: request values made safe to put in a line, and every line saying where it is from.

A value from a request may carry a line break or another control character, which would let
it forge a log line of its own (CWE-117). Every such value goes through ``loggable`` on its way
to the log, validated or not, so that no log line depends on a check made elsewhere.

With several processes behind a load balancer, a line is read beside the other processes'
lines: ``configure`` makes every line name the process (``pod``, its worker id) and, where
the work is a turn's, the turn, its conversation and its attempt
(``controller.contract.context``). ``text`` is one line each for a person; ``json`` is one
object each, for a collector. The access log keeps paths and drops query strings, since one
of this platform's paths carries an authorization code in one.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import UTC, datetime
from typing import Any

from robinauts.controller.contract import context

FORMATS = ("text", "json")
UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")
TEXT = "%(asctime)s %(levelname)s %(name)s [%(where)s] %(message)s"

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_LONGEST = 200


def loggable(value: str) -> str:
    """``value`` with its control characters replaced by ``?`` and cut to 200 characters."""
    return _CONTROL.sub("?", value[:_LONGEST])


class Where(logging.Filter):
    """Every record gets the process's worker id and the turn's ids, as attributes."""

    def __init__(self, pod: str) -> None:
        super().__init__()
        self._pod = pod

    def filter(self, record: logging.LogRecord) -> bool:
        record.pod = self._pod
        record.turn_id = context.turn_id.get()
        record.session_id = context.session_id.get()
        record.attempt = context.attempt.get()
        named = [f"pod={self._pod}"]
        if record.session_id is not None:
            named.append(f"conversation={record.session_id}")
        if record.turn_id is not None:
            named.append(f"turn={record.turn_id}")
        if record.attempt is not None:
            named.append(f"attempt={record.attempt}")
        record.where = " ".join(named)
        if record.name == "uvicorn.access" and isinstance(record.args, tuple):
            record.args = tuple(
                arg.split("?", 1)[0] if at == 2 and isinstance(arg, str) else arg
                for at, arg in enumerate(record.args)
            )
        return True


class JsonLines(logging.Formatter):
    """One JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:
        line: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "pod": getattr(record, "pod", None),
        }
        for key in ("session_id", "turn_id", "attempt"):
            value = getattr(record, key, None)
            if value is not None:
                line[key] = value
        if record.exc_info:
            line["exc"] = self.formatException(record.exc_info)
        return json.dumps(line, ensure_ascii=False)


def configure(log_format: str, pod: str) -> None:
    """The root logger at ``info``, on standard output, in that format, every line naming
    where it is from. ``ValueError`` for a format that is neither of ``FORMATS``."""
    if log_format not in FORMATS:
        raise ValueError(f"ROBINAUTS_LOG_FORMAT is {log_format!r}, not one of {FORMATS}")
    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(Where(pod))
    handler.setFormatter(JsonLines() if log_format == "json" else logging.Formatter(TEXT))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.INFO)
    # uvicorn's own lines go the same way, whatever configured its loggers before.
    for name in UVICORN_LOGGERS:
        logger = logging.getLogger(name)
        logger.handlers = []
        logger.propagate = True
        logger.disabled = False
