# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""What crosses the turn broker beside the controller's own records: the tasks a worker claims,
and the messages and events as a store keeps them and a worker writes them."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from robinauts.shared_domain.domain import DEFAULT_WORKER, Role

# --- tasks ----------------------------------------------------------------------


class TaskState(StrEnum):
    QUEUED = "queued"
    """Stored, and waiting for a worker to claim it."""
    RUNNING = "running"
    """Claimed by a worker, which holds its lease."""
    DONE = "done"
    """Needs no worker any more, however it ended."""


RUN_TURN = "run_turn"
"""The one kind of task: run a turn, named in the payload as ``turn_id``."""


@dataclass(frozen=True, slots=True)
class Task:
    """Work for a worker: what to do, and the lease of whoever does it."""

    id: uuid.UUID
    name: str
    payload: Mapping[str, Any]
    state: TaskState
    created_at: datetime
    lease_until: datetime
    """While queued, how long the task may wait; once claimed, the worker's lease."""
    claimed_at: datetime | None = None
    worker: str = DEFAULT_WORKER
    """The worker that may claim it: its agent's, when it was queued."""


# --- records, as a store keeps them and a worker writes them ---------------------

Document = Mapping[str, Any]
"""A message or an event, whole, as the encoder wrote it."""

Cursor = tuple[datetime, uuid.UUID]
"""Where a page of sessions ended: the ``(updated_at, id)`` of its last session."""


@dataclass(frozen=True, slots=True)
class StoredMessage:
    """A message as a store keeps it: the columns it orders and joins by, and the document."""

    id: uuid.UUID
    session_id: uuid.UUID
    parent_id: uuid.UUID | None
    role: Role
    created_at: datetime
    document: Document


@dataclass(frozen=True, slots=True)
class StoredEvent:
    """One of a turn's events: its position, its document, and when it expires."""

    position: int
    document: Document
    expires_at: datetime
