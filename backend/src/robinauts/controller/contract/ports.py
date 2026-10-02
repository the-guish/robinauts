# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The controller port: the operations of ``docs/architecture/controller.md``, as types."""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator
from datetime import datetime

from robinauts.controller.contract.domain import (
    AgentListing,
    ApiToken,
    Identity,
    ModelListing,
    NumberedEvent,
    OpenedSession,
    PendingLogin,
    Session,
    SessionPage,
    TurnStarted,
    User,
    UserSession,
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


class Credentials(ABC):
    """Where sign-in keeps its records: user sessions, sign-ins in progress and API tokens
    (``docs/specs/sign-in.md``). A secret never reaches it: a record holds the SHA-256 hex of
    one, and a lookup is by that hash. Every id and every time comes from the caller, and no
    clock is read here. Every operation is one statement, so two callers at once never both
    take what is taken once."""

    @abstractmethod
    async def add_user_session(self, session: UserSession) -> None:
        """Its user is one the store has."""
        raise NotImplementedError

    @abstractmethod
    async def resolve_user_session(self, secret_hash: str, now: datetime) -> User | None:
        """The user of the session with that hash, or ``None`` when there is none or its
        ``expires_at`` is not after ``now``."""
        raise NotImplementedError

    @abstractmethod
    async def delete_user_session(self, secret_hash: str) -> bool:
        """Delete the session with that hash: true when there was one."""
        raise NotImplementedError

    @abstractmethod
    async def add_pending_login(self, login: PendingLogin, now: datetime) -> None:
        """Store it after deleting every pending login whose ``expires_at`` is not after
        ``now``, so the table holds the last ten minutes and no sweep is needed yet."""
        raise NotImplementedError

    @abstractmethod
    async def take_pending_login(self, state_hash: str, now: datetime) -> PendingLogin | None:
        """Delete it and return it, once; ``None`` when it is gone or its ``expires_at`` is not
        after ``now``, and an expired one is deleted either way."""
        raise NotImplementedError

    @abstractmethod
    async def add_api_token(self, token: ApiToken) -> None:
        """Its user is one the store has."""
        raise NotImplementedError

    @abstractmethod
    async def resolve_api_token(self, secret_hash: str, now: datetime) -> User | None:
        """The owner of the token with that hash, or ``None`` when there is none or its
        ``expires_at`` is not after ``now``."""
        raise NotImplementedError

    @abstractmethod
    async def api_tokens_of(self, user_id: uuid.UUID) -> list[ApiToken]:
        """The user's tokens, oldest first, ties broken by id."""
        raise NotImplementedError

    @abstractmethod
    async def delete_api_token(self, user_id: uuid.UUID, token_id: uuid.UUID) -> bool:
        """Only the owner's: true when that user had that token."""
        raise NotImplementedError
