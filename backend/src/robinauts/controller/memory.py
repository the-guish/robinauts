# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The store over dicts, in this process."""

from __future__ import annotations

import asyncio
import uuid

from robinauts.controller.contract.domain import (
    ActiveTurn,
    Conversation,
    Message,
    NumberedEvent,
    TurnEvent,
    User,
)
from robinauts.controller.store import Store


class MemoryStore(Store):
    def __init__(self) -> None:
        self._users: dict[tuple[str, str], User] = {}
        self._conversations: dict[uuid.UUID, Conversation] = {}
        self._messages: dict[uuid.UUID, list[Message]] = {}
        self._events: dict[uuid.UUID, list[NumberedEvent]] = {}
        self._follows: dict[uuid.UUID, uuid.UUID] = {}
        self._changed = asyncio.Condition()

    async def user_by_identity(self, provider: str, subject: str) -> User | None:
        return self._users.get((provider, subject))

    async def add_user(self, user: User) -> None:
        self._users[(user.provider, user.subject)] = user

    async def add_conversation(self, conversation: Conversation) -> None:
        self._conversations[conversation.id] = conversation
        self._messages[conversation.id] = []
        self._events[conversation.id] = []

    async def get_conversation(self, conversation_id: uuid.UUID) -> Conversation | None:
        return self._conversations.get(conversation_id)

    async def update_conversation(self, conversation: Conversation) -> None:
        self._conversations[conversation.id] = conversation

    async def delete_conversation(self, conversation_id: uuid.UUID) -> None:
        del self._conversations[conversation_id]
        del self._messages[conversation_id]
        del self._events[conversation_id]
        self._follows.pop(conversation_id, None)

    async def conversations_of(self, owner_id: uuid.UUID) -> list[Conversation]:
        owned = [c for c in self._conversations.values() if c.owner_id == owner_id]
        return sorted(owned, key=lambda c: c.updated_at, reverse=True)

    async def add_message(self, message: Message) -> None:
        self._messages[message.conversation_id].append(message)

    async def messages_of(self, conversation_id: uuid.UUID) -> list[Message]:
        return list(self._messages[conversation_id])

    async def start_turn(self, conversation_id: uuid.UUID, follows: uuid.UUID) -> None:
        self._follows[conversation_id] = follows
        self._events[conversation_id] = []

    async def append_event(self, conversation_id: uuid.UUID, event: TurnEvent) -> NumberedEvent:
        events = self._events[conversation_id]
        numbered = NumberedEvent(len(events) + 1, event)
        events.append(numbered)
        async with self._changed:
            self._changed.notify_all()
        return numbered

    async def events_after(self, conversation_id: uuid.UUID, position: int) -> list[NumberedEvent]:
        return self._events[conversation_id][position:]

    async def end_turn(self, conversation_id: uuid.UUID) -> None:
        del self._follows[conversation_id]
        async with self._changed:
            self._changed.notify_all()

    async def active_turn(self, conversation_id: uuid.UUID) -> ActiveTurn | None:
        follows = self._follows.get(conversation_id)
        if follows is None:
            return None
        return ActiveTurn(follows, len(self._events[conversation_id]))

    async def wait_for_events(self, conversation_id: uuid.UUID, after: int) -> None:
        async with self._changed:
            await self._changed.wait_for(
                lambda: len(self._events[conversation_id]) > after
                or conversation_id not in self._follows
            )
