# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""What a log line is about: the turn and the conversation of the work under way.

Set by the controller where a turn's work begins, and carried by the task doing it, so that
every line logged on the way, an engine's included, can name them. Read by whoever formats
the log; nothing decides anything by them.
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar

turn_id: ContextVar[str | None] = ContextVar("robinauts_turn_id", default=None)
session_id: ContextVar[str | None] = ContextVar("robinauts_session_id", default=None)
attempt: ContextVar[int | None] = ContextVar("robinauts_attempt", default=None)


def about(session: uuid.UUID, turn: uuid.UUID, held_at: int | None = None) -> None:
    """From here on, in this task, the work is that turn's."""
    session_id.set(str(session))
    turn_id.set(str(turn))
    attempt.set(held_at)
