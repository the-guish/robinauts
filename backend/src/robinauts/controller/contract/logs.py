# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""What a log line is about: the turn and the conversation, wherever in a process it is
written. The controller binds them while it runs a turn, and web while it answers a request
about one; the formatter web installs puts them, and the process's worker id, on every line.
Context variables, so that a task inherits what was bound where it was made, and no two
turns see each other's."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

turn_id: ContextVar[str | None] = ContextVar("robinauts_turn_id", default=None)
session_id: ContextVar[str | None] = ContextVar("robinauts_session_id", default=None)


@contextmanager
def about(
    *, session: uuid.UUID | str | None = None, turn: uuid.UUID | str | None = None
) -> Iterator[None]:
    """Log lines written inside are about that conversation and that turn."""
    tokens = []
    if session is not None:
        tokens.append((session_id, session_id.set(str(session))))
    if turn is not None:
        tokens.append((turn_id, turn_id.set(str(turn))))
    try:
        yield
    finally:
        for variable, token in reversed(tokens):
            variable.reset(token)
