# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The in-memory store keeps what it is given and hands it back."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from aio import asyncio_test
from robinauts.controller.contract.domain import (
    ActiveTurn,
    Conversation,
    Message,
    NumberedEvent,
    Role,
    TextPart,
    TextPiece,
    TurnEnded,
    TurnState,
    User,
)
from robinauts.controller.memory import MemoryStore

NOW = datetime(2026, 1, 1, tzinfo=UTC)


@asyncio_test
async def test_a_user_is_found_by_identity() -> None:
    store = MemoryStore()
    user = User(uuid.uuid4(), "local", "me")
    await store.add_user(user)
    assert await store.user_by_identity("local", "me") == user
    assert await store.user_by_identity("local", "you") is None


@asyncio_test
async def test_conversations_and_messages_read_back() -> None:
    store = MemoryStore()
    owner = uuid.uuid4()
    older = Conversation(uuid.uuid4(), owner, "a", NOW, NOW)
    newer = Conversation(uuid.uuid4(), owner, "a", NOW, NOW.replace(hour=1))
    await store.add_conversation(older)
    await store.add_conversation(newer)
    question = Message(uuid.uuid4(), older.id, None, Role.USER, (TextPart("hi"),), NOW)
    await store.add_message(question)
    assert await store.get_conversation(older.id) == older
    assert await store.conversations_of(owner) == [newer, older]
    assert await store.messages_of(older.id) == [question]


@asyncio_test
async def test_a_turn_numbers_its_events_and_ends() -> None:
    store = MemoryStore()
    conversation = Conversation(uuid.uuid4(), uuid.uuid4(), "a", NOW, NOW)
    await store.add_conversation(conversation)
    question = uuid.uuid4()
    await store.start_turn(conversation.id, question)
    piece = TextPiece(uuid.uuid4(), "hello")
    ended = TurnEnded(TurnState.FINISHED)
    assert await store.append_event(conversation.id, piece) == NumberedEvent(1, piece)
    assert await store.active_turn(conversation.id) == ActiveTurn(question, 1)
    await store.append_event(conversation.id, ended)
    await store.wait_for_events(conversation.id, 1)
    assert await store.events_after(conversation.id, 1) == [NumberedEvent(2, ended)]
    await store.end_turn(conversation.id)
    assert await store.active_turn(conversation.id) is None
