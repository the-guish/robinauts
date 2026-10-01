# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The controller port: the operations of ``docs/architecture/controller.md``, as types."""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator

from robinauts.controller.contract.domain import (
    AgentListing,
    Conversation,
    ConversationPage,
    Identity,
    ModelListing,
    NumberedEvent,
    OpenedConversation,
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
    async def list_conversations(
        self, user: User, *, limit: int, cursor: str | None = None
    ) -> ConversationPage:
        """``InvalidValueError`` for a limit out of range or a cursor that does not parse."""
        raise NotImplementedError

    @abstractmethod
    async def open_conversation(self, user: User, conversation_id: uuid.UUID) -> OpenedConversation:
        """``ConversationNotFoundError`` for one the user does not have."""
        raise NotImplementedError

    @abstractmethod
    async def rename_conversation(
        self, user: User, conversation_id: uuid.UUID, title: str
    ) -> Conversation:
        """``ConversationNotFoundError``; ``InvalidValueError`` for a title out of bounds."""
        raise NotImplementedError

    @abstractmethod
    async def delete_conversation(self, user: User, conversation_id: uuid.UUID) -> None:
        """``ConversationNotFoundError``. The engine's memory goes with the records."""
        raise NotImplementedError

    @abstractmethod
    async def fork_conversation(
        self, user: User, conversation_id: uuid.UUID, *, at_message: uuid.UUID
    ) -> Conversation:
        """``ConversationNotFoundError``; ``MessageNotFoundError`` for a message not on it."""
        raise NotImplementedError

    @abstractmethod
    async def start_conversation(
        self, user: User, *, agent: str, model: str, text: str
    ) -> TurnStarted:
        """``UnknownAgentError``, ``UnknownModelError``; ``InvalidValueError`` for empty text."""
        raise NotImplementedError

    @abstractmethod
    async def send_message(
        self,
        user: User,
        conversation_id: uuid.UUID,
        *,
        parent_id: uuid.UUID,
        model: str,
        text: str,
    ) -> TurnStarted:
        """``ConversationNotFoundError``, ``MessageNotFoundError`` for the parent,
        ``UnknownModelError``, ``TurnActiveError`` while a turn runs, ``InvalidValueError``
        for empty text."""
        raise NotImplementedError

    @abstractmethod
    async def regenerate_answer(
        self, user: User, conversation_id: uuid.UUID, *, question_id: uuid.UUID, model: str
    ) -> TurnStarted:
        """``ConversationNotFoundError``, ``MessageNotFoundError`` for a question not on it,
        ``UnknownModelError``, ``TurnActiveError`` while a turn runs."""
        raise NotImplementedError

    @abstractmethod
    async def cancel_turn(self, user: User, conversation_id: uuid.UUID) -> None:
        """``ConversationNotFoundError``; ``NoActiveTurnError`` when nothing runs."""
        raise NotImplementedError

    @abstractmethod
    def watch_turn(
        self, user: User, conversation_id: uuid.UUID, *, after: int = 0
    ) -> AsyncGenerator[NumberedEvent, None]:
        """Not a coroutine: the refusals happen inside the generator.

        Yields the turn's events past ``after`` until ``TurnEnded``, then stops.
        ``ConversationNotFoundError``; ``NoActiveTurnError`` when nothing runs and nothing
        ended past ``after``.
        """
        raise NotImplementedError

    @abstractmethod
    async def sweep(self) -> None:
        raise NotImplementedError
