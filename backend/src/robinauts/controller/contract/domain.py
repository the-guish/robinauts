# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""What crosses the controller port, as plain data (``docs/architecture/controller.md``), beside
the records of ``shared_domain``."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from robinauts.shared_domain.domain import (
    ControllerError,
    Message,
    Session,
    Turn,
    TurnEvent,
)

# --- errors -------------------------------------------------------------------


class InvalidValueError(ControllerError, ValueError):
    pass


class MessageNotFoundError(ControllerError):
    pass


class UnknownAgentError(ControllerError):
    pass


class UnknownModelError(ControllerError):
    pass


class ConfigError(ControllerError):
    """A configuration that cannot be used; the message lists every problem found."""


class TurnActiveError(ControllerError):
    """A turn asked for while the session already has one running."""


class NoActiveTurnError(ControllerError):
    pass


# --- users --------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Identity:
    """A person as oneberth holds them (``docs/specs/identity.md``): a provider and a subject,
    and what their last sign-in said. Robinauts keeps its own row of them, under oneberth's id
    for them."""

    provider: str
    subject: str
    name: str | None = None
    email: str | None = None
    hosted_domain: str | None = None
    groups: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class User:
    """A person, under the id oneberth gives them; ``name``, ``email``, ``hosted_domain`` and
    ``groups`` are what their last sign-in said, as oneberth told it."""

    id: uuid.UUID
    provider: str
    subject: str
    name: str | None = None
    email: str | None = None
    created_at: datetime | None = None
    hosted_domain: str | None = None
    groups: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AgentListing:
    id: str
    title: str
    default_model: str
    models: tuple[str, ...]
    """The models its turns may run on, in order, its default first."""


@dataclass(frozen=True, slots=True)
class ModelListing:
    id: str
    title: str


# --- sessions -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SessionPage:
    sessions: tuple[Session, ...]
    cursor: str | None = None
    running: frozenset[uuid.UUID] = frozenset()
    """Those of ``sessions`` that have an active turn."""


@dataclass(frozen=True, slots=True)
class ActiveTurn:
    """The turn a session is in the middle of: which, what it answers, and where its events are."""

    turn_id: uuid.UUID
    follows: uuid.UUID
    position: int


@dataclass(frozen=True, slots=True)
class OpenedSession:
    """One moment of a session: its record, the thread it shows, oldest first, and its turn."""

    session: Session
    messages: tuple[Message, ...]
    active: ActiveTurn | None = None
    ended_badly: Turn | None = None
    """The session's last turn, when it failed, was cancelled or was interrupted."""


# --- turns --------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class NumberedEvent:
    """A turn's event at its position, which is what a watcher asks to continue from."""

    position: int
    event: TurnEvent
