# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The controller port: the operations of ``docs/architecture/controller.md``, as types."""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator

from robinauts.controller.contract.domain import (
    AgentListing,
    Identity,
    ModelListing,
    NumberedEvent,
    OpenedSession,
    Session,
    SessionPage,
    TurnStarted,
    User,
)


class Controller(ABC):
    @abstractmethod
    async def open(self) -> None:
        raise NotImplementedError

    @abstractmethod
    async def close(self) -> None:
        raise NotImplementedError

    @abstractmethod
    async def ensure_user(self, identity: Identity) -> User:
        raise NotImplementedError

    @abstractmethod
    async def list_agents(self) -> tuple[AgentListing, ...]:
        raise NotImplementedError

    @abstractmethod
    async def list_models(self) -> tuple[ModelListing, ...]:
        raise NotImplementedError

    @abstractmethod
    async def list_sessions(
        self, user: User, *, limit: int, cursor: str | None = None
    ) -> SessionPage:
        """``InvalidValueError`` for a limit out of range or a cursor that does not parse."""
        raise NotImplementedError

    @abstractmethod
    async def open_session(self, user: User, session_id: uuid.UUID) -> OpenedSession:
        """``SessionNotFoundError`` for one the user does not have."""
        raise NotImplementedError

    @abstractmethod
    async def rename_session(self, user: User, session_id: uuid.UUID, title: str) -> Session:
        """``SessionNotFoundError``; ``InvalidValueError`` for a title out of bounds."""
        raise NotImplementedError

    @abstractmethod
    async def delete_session(self, user: User, session_id: uuid.UUID) -> None:
        """``SessionNotFoundError``. The engine's memory goes with the records."""
        raise NotImplementedError

    @abstractmethod
    async def fork_session(
        self, user: User, session_id: uuid.UUID, *, at_message: uuid.UUID
    ) -> Session:
        """``SessionNotFoundError``; ``MessageNotFoundError`` for a message not on it."""
        raise NotImplementedError

    @abstractmethod
    async def start_session(self, user: User, *, agent: str, model: str, text: str) -> TurnStarted:
        """``UnknownAgentError``, ``UnknownModelError``; ``InvalidValueError`` for empty text."""
        raise NotImplementedError

    @abstractmethod
    async def send_message(
        self,
        user: User,
        session_id: uuid.UUID,
        *,
        parent_id: uuid.UUID,
        model: str,
        text: str,
    ) -> TurnStarted:
        """``SessionNotFoundError``, ``MessageNotFoundError`` for the parent,
        ``UnknownModelError``, ``TurnActiveError`` while a turn runs, ``InvalidValueError``
        for empty text."""
        raise NotImplementedError

    @abstractmethod
    async def regenerate_answer(
        self, user: User, session_id: uuid.UUID, *, question_id: uuid.UUID, model: str
    ) -> TurnStarted:
        """``SessionNotFoundError``, ``MessageNotFoundError`` for a question not on it,
        ``UnknownModelError``, ``TurnActiveError`` while a turn runs."""
        raise NotImplementedError

    @abstractmethod
    async def cancel_turn(
        self, user: User, session_id: uuid.UUID, turn_id: uuid.UUID | None = None
    ) -> None:
        """``SessionNotFoundError``; ``NoActiveTurnError`` when nothing runs. ``turn_id``
        ``None`` names the session's running turn."""
        raise NotImplementedError

    @abstractmethod
    def watch_turn(
        self,
        user: User,
        session_id: uuid.UUID,
        turn_id: uuid.UUID | None = None,
        *,
        after: int = 0,
    ) -> AsyncGenerator[NumberedEvent, None]:
        """Not a coroutine: the refusals happen inside the generator.

        Yields the turn's events past ``after`` until ``TurnEnded``, then stops. ``turn_id``
        ``None`` names the session's latest turn. ``SessionNotFoundError``;
        ``NoActiveTurnError`` for a turn the session does not have.
        """
        raise NotImplementedError

    @abstractmethod
    async def sweep(self) -> None:
        raise NotImplementedError
