# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""A run: the work an agent does to answer one user message.

A record, not a request (``docs/specs/runs.md``). It executes in the
background, everything it produces is persisted as it is produced, and it
outlives the request that started it: if the tab closes, the answer is in the
conversation when its author comes back.

The states are the spec's six. Which of them may follow which is a pure
function in ``robinauts.legacy.core.runs``, with a conversation's "at most one active
run"; a record here knows only what it must hold to be in the state it says it
is in.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from robinauts.legacy.domain.agents import Engine, checked_config_id
from robinauts.legacy.domain.conversation import Provenance
from robinauts.legacy.domain.errors import InvalidValueError
from robinauts.legacy.domain.values import checked_instant, checked_text, checked_uuid, describe

MAX_RUN_ERROR_CHARS = 2_000
"""How much of what went wrong is kept on the run.

Bounded because it is whatever a provider, a framework or a traceback said,
and a run record is not a log.
"""


class RunState(StrEnum):
    """Where a run is (``docs/specs/runs.md``)."""

    RUNNING = "running"
    """Executing in a backend process."""
    WAITING = "waiting"
    """Suspended on a tool call with no result; nothing is held in memory."""
    FINISHED = "finished"
    """Completed; its messages are in the conversation."""
    FAILED = "failed"
    """Ended with an error, which is recorded on the run."""
    CANCELLED = "cancelled"
    """Stopped by the conversation's author."""
    INTERRUPTED = "interrupted"
    """Its process went away while it was running."""


ACTIVE_RUN_STATES: frozenset[RunState] = frozenset({RunState.RUNNING, RunState.WAITING})
"""The states a run is still going in. A conversation has at most one."""

ENDED_RUN_STATES: frozenset[RunState] = frozenset(RunState) - ACTIVE_RUN_STATES
"""The states a run is over in. It never leaves one of them."""

FAULTED_RUN_STATES: frozenset[RunState] = frozenset(
    {RunState.FAILED, RunState.CANCELLED, RunState.INTERRUPTED}
)
"""The ways a run can end other than by finishing; only these carry an error."""


@dataclass(frozen=True, slots=True)
class Run:
    """One turn's work, as the database holds it.

    The three times are checked for being times and **not** against each
    other. A wall clock steps backwards now and then -- an NTP correction is
    enough -- and a run that could not record its own end because of it would
    lose an answer that had already been produced.
    ``robinauts.legacy.core.runs.transition`` is what keeps the records it builds in
    order, by clamping each stamp to the one before it.
    """

    id: uuid.UUID
    conversation_id: uuid.UUID
    message_id: uuid.UUID
    """The user message this run answers."""
    agent: str
    engine: Engine
    model: str
    state: RunState
    created_at: datetime
    started_at: datetime | None = None
    """When a process took it up; ``None`` until one has."""
    finished_at: datetime | None = None
    """When it ended. Set in every ended state, and in no other."""
    error: str | None = None
    """What went wrong. Required when it failed, and only for a bad end."""

    def __post_init__(self) -> None:
        checked_uuid(self.id, "a run's id")
        checked_uuid(self.conversation_id, "a run's conversation id")
        checked_uuid(self.message_id, "the message a run answers")
        checked_config_id(self.agent, "an agent's id")
        checked_config_id(self.model, "a model's id")
        if not isinstance(self.engine, Engine):
            raise InvalidValueError(f"an engine is an Engine, not {describe(self.engine)}")
        if not isinstance(self.state, RunState):
            raise InvalidValueError(f"a run's state is a RunState, not {describe(self.state)}")
        checked_instant(self.created_at, "created_at")
        if self.started_at is not None:
            checked_instant(self.started_at, "started_at")
        if self.finished_at is not None:
            checked_instant(self.finished_at, "finished_at")
        # An ended run has an end, and an active one has not ended: without
        # this, a `finished` run with no `finished_at` would sit in every
        # listing sorted by when it ended, at whatever place a null falls.
        if self.state in ENDED_RUN_STATES and self.finished_at is None:
            raise InvalidValueError(f"a {self.state.value} run records when it ended")
        if self.state in ACTIVE_RUN_STATES and self.finished_at is not None:
            raise InvalidValueError(f"a {self.state.value} run has not ended")
        if self.state is RunState.FAILED and not self.error:
            raise InvalidValueError("a failed run records what went wrong")
        if self.error is not None:
            checked_text(self.error, "a run's error", MAX_RUN_ERROR_CHARS)
            if self.state not in FAULTED_RUN_STATES:
                raise InvalidValueError(f"a {self.state.value} run has no error to record")

    @property
    def is_active(self) -> bool:
        """Whether this run is still going, which is what blocks a new one."""
        return self.state in ACTIVE_RUN_STATES

    @property
    def provenance(self) -> Provenance:
        """What a message this run produced records."""
        return Provenance(agent=self.agent, engine=self.engine, model=self.model, run_id=self.id)
