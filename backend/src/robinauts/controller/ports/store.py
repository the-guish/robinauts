# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""What the controller keeps: users, sessions, messages, and each turn's numbered events."""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod

from robinauts.controller.contract.domain import (
    ActiveTurn,
    Message,
    NumberedEvent,
    Session,
    TurnEvent,
    User,
)


class Store(ABC):
    @abstractmethod
    async def user_by_identity(self, provider: str, subject: str) -> User | None: ...

    @abstractmethod
    async def add_user(self, user: User) -> None: ...

    @abstractmethod
    async def add_session(self, session: Session) -> None: ...

    @abstractmethod
    async def get_session(self, session_id: uuid.UUID) -> Session | None: ...

    @abstractmethod
    async def update_session(self, session: Session) -> None: ...

    @abstractmethod
    async def delete_session(self, session_id: uuid.UUID) -> None: ...

    @abstractmethod
    async def sessions_of(self, owner_id: uuid.UUID) -> list[Session]:
        """Newest updated first."""

    @abstractmethod
    async def add_message(self, message: Message) -> None: ...

    @abstractmethod
    async def messages_of(self, session_id: uuid.UUID) -> list[Message]: ...

    @abstractmethod
    async def start_turn(self, session_id: uuid.UUID, follows: uuid.UUID) -> None:
        """The turn's events start again at position 1."""

    @abstractmethod
    async def append_event(self, session_id: uuid.UUID, event: TurnEvent) -> NumberedEvent: ...

    @abstractmethod
    async def events_after(self, session_id: uuid.UUID, position: int) -> list[NumberedEvent]: ...

    @abstractmethod
    async def end_turn(self, session_id: uuid.UUID) -> None: ...

    @abstractmethod
    async def active_turn(self, session_id: uuid.UUID) -> ActiveTurn | None: ...

    @abstractmethod
    async def wait_for_events(self, session_id: uuid.UUID, after: int) -> None:
        """Returns once events past ``after`` exist or no turn is active."""
