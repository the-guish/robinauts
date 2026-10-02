# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The store over dicts, in this process."""

from __future__ import annotations

import asyncio
import uuid

from robinauts.controller.contract.domain import (
    ActiveTurn,
    Message,
    NumberedEvent,
    Session,
    TurnEvent,
    User,
)
from robinauts.controller.ports.store import Store


class MemoryStore(Store):
    def __init__(self) -> None:
        self._users: dict[tuple[str, str], User] = {}
        self._sessions: dict[uuid.UUID, Session] = {}
        self._messages: dict[uuid.UUID, list[Message]] = {}
        self._events: dict[uuid.UUID, list[NumberedEvent]] = {}
        self._turns: dict[uuid.UUID, tuple[uuid.UUID, uuid.UUID]] = {}
        """A session's running turn: its id and the question it answers."""
        self._changed = asyncio.Condition()

    async def user_by_identity(self, provider: str, subject: str) -> User | None:
        return self._users.get((provider, subject))

    async def add_user(self, user: User) -> None:
        self._users[(user.provider, user.subject)] = user

    async def add_session(self, session: Session) -> None:
        self._sessions[session.id] = session
        self._messages[session.id] = []
        self._events[session.id] = []

    async def get_session(self, session_id: uuid.UUID) -> Session | None:
        return self._sessions.get(session_id)

    async def update_session(self, session: Session) -> None:
        self._sessions[session.id] = session

    async def delete_session(self, session_id: uuid.UUID) -> None:
        del self._sessions[session_id]
        del self._messages[session_id]
        del self._events[session_id]
        self._turns.pop(session_id, None)

    async def sessions_of(self, owner_id: uuid.UUID) -> list[Session]:
        owned = [c for c in self._sessions.values() if c.owner_id == owner_id]
        return sorted(owned, key=lambda c: c.updated_at, reverse=True)

    async def add_message(self, message: Message) -> None:
        self._messages[message.session_id].append(message)

    async def messages_of(self, session_id: uuid.UUID) -> list[Message]:
        return list(self._messages[session_id])

    async def start_turn(
        self, session_id: uuid.UUID, follows: uuid.UUID, turn_id: uuid.UUID
    ) -> None:
        self._turns[session_id] = (turn_id, follows)
        self._events[session_id] = []

    async def append_event(self, session_id: uuid.UUID, event: TurnEvent) -> NumberedEvent:
        events = self._events[session_id]
        numbered = NumberedEvent(len(events) + 1, event)
        events.append(numbered)
        async with self._changed:
            self._changed.notify_all()
        return numbered

    async def events_after(self, session_id: uuid.UUID, position: int) -> list[NumberedEvent]:
        return self._events[session_id][position:]

    async def end_turn(self, session_id: uuid.UUID) -> None:
        del self._turns[session_id]
        async with self._changed:
            self._changed.notify_all()

    async def active_turn(self, session_id: uuid.UUID) -> ActiveTurn | None:
        running = self._turns.get(session_id)
        if running is None:
            return None
        turn_id, follows = running
        return ActiveTurn(turn_id, follows, len(self._events[session_id]))

    async def wait_for_events(self, session_id: uuid.UUID, after: int) -> None:
        async with self._changed:
            await self._changed.wait_for(
                lambda: len(self._events[session_id]) > after or session_id not in self._turns
            )
