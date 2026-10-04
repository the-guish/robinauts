# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Log lines: what they say about where they come from, and request values made safe.

Every line names the pod that wrote it and, while a turn runs, the conversation, the turn and
its attempt (``controller.contract.context``), so that the lines of one turn can be followed
across replicas. ``ROBINAUTS_LOG_FORMAT`` is ``text`` (the default) or ``json``, one object a
line.

A value from a request may carry a line break or another control character, which would let
it forge a log line of its own (CWE-117). Every such value goes through ``loggable`` on its way
to the log, validated or not, so that no log line depends on a check made elsewhere.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime

from robinauts.controller.contract.context import LOG_CONTEXT

FORMAT_VARIABLE = "ROBINAUTS_LOG_FORMAT"
TEXT = "%(asctime)s %(levelname)s %(name)s pod=%(pod)s%(about)s %(message)s"

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_LONGEST = 200


def loggable(value: str) -> str:
    """``value`` with its control characters replaced by ``?`` and cut to 200 characters."""
    return _CONTROL.sub("?", value[:_LONGEST])


class Context(logging.Filter):
    """Puts the pod, and the turn being run if any, on every record."""

    def __init__(self, pod: str) -> None:
        super().__init__()
        self._pod = pod

    def filter(self, record: logging.LogRecord) -> bool:
        record.pod = self._pod
        about = LOG_CONTEXT.get()
        record.conversation = None if about is None else about.conversation
        record.turn = None if about is None else about.turn
        record.attempt = None if about is None else about.attempt
        record.about = (
            ""
            if about is None
            else f" conversation={about.conversation} turn={about.turn} attempt={about.attempt}"
        )
        return True


class JsonLines(logging.Formatter):
    """One JSON object a line."""

    def format(self, record: logging.LogRecord) -> str:
        line = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "pod": getattr(record, "pod", None),
        }
        for key in ("conversation", "turn", "attempt"):
            if getattr(record, key, None) is not None:
                line[key] = getattr(record, key)
        if record.exc_info:
            line["exc"] = self.formatException(record.exc_info)
        return json.dumps(line, ensure_ascii=False)


def configure(pod: str, format: str = "text") -> None:
    """The root logger at INFO, its lines naming the pod and the turn, as text or JSON."""
    handler = logging.StreamHandler()
    handler.addFilter(Context(pod))
    handler.setFormatter(JsonLines() if format == "json" else logging.Formatter(TEXT))
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
