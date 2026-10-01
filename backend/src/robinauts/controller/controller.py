# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The controller. An operation not yet implemented raises ``NotImplementedError``."""

from __future__ import annotations

import uuid
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
from robinauts.controller.contract.ports import Controller


class RobinautsController(Controller):
    async def open(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def ensure_user(self, identity: Identity) -> User:
        raise NotImplementedError("ensure_user")

    async def list_agents(self) -> tuple[AgentListing, ...]:
        raise NotImplementedError("list_agents")

    async def list_models(self) -> tuple[ModelListing, ...]:
        raise NotImplementedError("list_models")

    async def list_conversations(
        self, user: User, *, limit: int, cursor: str | None = None
    ) -> ConversationPage:
        raise NotImplementedError("list_conversations")

    async def open_conversation(self, user: User, conversation_id: uuid.UUID) -> OpenedConversation:
        raise NotImplementedError("open_conversation")

    async def rename_conversation(
        self, user: User, conversation_id: uuid.UUID, title: str
    ) -> Conversation:
        raise NotImplementedError("rename_conversation")

    async def delete_conversation(self, user: User, conversation_id: uuid.UUID) -> None:
        raise NotImplementedError("delete_conversation")

    async def fork_conversation(
        self, user: User, conversation_id: uuid.UUID, *, at_message: uuid.UUID
    ) -> Conversation:
        raise NotImplementedError("fork_conversation")

    async def start_conversation(
        self, user: User, *, agent: str, model: str, text: str
    ) -> TurnStarted:
        raise NotImplementedError("start_conversation")

    async def send_message(
        self,
        user: User,
        conversation_id: uuid.UUID,
        *,
        parent_id: uuid.UUID,
        model: str,
        text: str,
    ) -> TurnStarted:
        raise NotImplementedError("send_message")

    async def regenerate_answer(
        self, user: User, conversation_id: uuid.UUID, *, question_id: uuid.UUID, model: str
    ) -> TurnStarted:
        raise NotImplementedError("regenerate_answer")

    async def cancel_turn(self, user: User, conversation_id: uuid.UUID) -> None:
        raise NotImplementedError("cancel_turn")

    async def watch_turn(
        self, user: User, conversation_id: uuid.UUID, *, after: int = 0
    ) -> AsyncGenerator[NumberedEvent, None]:
        raise NotImplementedError("watch_turn")
        yield  # makes this the generator the port declares

    async def sweep(self) -> None:
        raise NotImplementedError("sweep")
