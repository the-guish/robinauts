# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""What a log line is about: the conversation and the turn a runner works on.

Set by the controller in each runner's own task, so that every line written while it runs,
the engines' included, can say which turn it is about; read by whoever formats the lines.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class LogContext:
    conversation: str
    turn: str
    attempt: int


LOG_CONTEXT: ContextVar[LogContext | None] = ContextVar("robinauts_log_context", default=None)
