# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The controller. An operation not yet implemented raises ``NotImplementedError``."""

from __future__ import annotations

import asyncio
import dataclasses
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime

from robinauts.agent_engines.contract.ports import AgentEngine, installed
from robinauts.controller.application.engines import (
    SecretLookup,
    build_engines,
    engine_settings,
    engine_storage,
)
from robinauts.controller.application.turns import run_turn
from robinauts.controller.contract.domain import (
    AgentListing,
    Config,
    Identity,
    Message,
    ModelListing,
    NumberedEvent,
    OpenedSession,
    Role,
    Session,
    SessionPage,
    StorageConfig,
    TextPart,
    TurnEnded,
    TurnStarted,
    User,
)
from robinauts.controller.contract.ports import Controller
from robinauts.controller.ports.store import Store


class RobinautsController(Controller):
    def __init__(
        self, config: Config, *, store: Store, storage: StorageConfig, secret_for: SecretLookup
    ) -> None:
        self._config = config
        self._store = store
        self._storage = storage
        self._secret_for = secret_for
        self._engines: dict[str, AgentEngine] = {}
        self._turns: dict[uuid.UUID, asyncio.Task[None]] = {}

    async def open(self) -> None:
        settings = engine_settings(self._config, self._secret_for)
        self._engines = await build_engines(
            self._config, settings, engine_storage(self._storage, None), installed()
        )

    async def close(self) -> None:
        self._engines = {}

    async def ensure_user(self, identity: Identity) -> User:
        user = await self._store.user_by_identity(identity.provider, identity.subject)
        if user is None:
            user = User(
                id=uuid.uuid4(),
                provider=identity.provider,
                subject=identity.subject,
                name=identity.name,
                email=identity.email,
                created_at=datetime.now(UTC),
            )
            await self._store.add_user(user)
        return user

    async def list_agents(self) -> tuple[AgentListing, ...]:
        return tuple(
            AgentListing(a.id, a.title, default_model=a.model) for a in self._config.agents.values()
        )

    async def list_models(self) -> tuple[ModelListing, ...]:
        return tuple(ModelListing(m.id, m.title or m.id) for m in self._config.models.values())

    async def list_sessions(
        self, user: User, *, limit: int, cursor: str | None = None
    ) -> SessionPage:
        sessions = await self._store.sessions_of(user.id)
        return SessionPage(tuple(sessions[:limit]), cursor=None)

    async def open_session(self, user: User, session_id: uuid.UUID) -> OpenedSession:
        session = await self._store.get_session(session_id)
        messages = await self._store.messages_of(session_id)
        by_id = {m.id: m for m in messages}
        thread = [messages[-1]]
        while thread[-1].parent_id is not None:
            thread.append(by_id[thread[-1].parent_id])
        active = await self._store.active_turn(session_id)
        return OpenedSession(session, tuple(reversed(thread)), active)

    async def rename_session(self, user: User, session_id: uuid.UUID, title: str) -> Session:
        session = await self._store.get_session(session_id)
        renamed = dataclasses.replace(session, title=title, updated_at=datetime.now(UTC))
        await self._store.update_session(renamed)
        return renamed

    async def delete_session(self, user: User, session_id: uuid.UUID) -> None:
        session = await self._store.get_session(session_id)
        engine = self._engines[self._config.agents[session.agent].engine]
        await engine.forget(session_id)
        await self._store.delete_session(session_id)

    async def fork_session(
        self, user: User, session_id: uuid.UUID, *, at_message: uuid.UUID
    ) -> Session:
        raise NotImplementedError("fork_session")

    async def start_session(self, user: User, *, agent: str, model: str, text: str) -> TurnStarted:
        agent_config = self._config.agents[agent]
        now = datetime.now(UTC)
        session = Session(
            uuid.uuid4(), owner_id=user.id, agent=agent, created_at=now, updated_at=now
        )
        question = Message(
            uuid.uuid4(),
            session.id,
            parent_id=None,
            role=Role.USER,
            parts=(TextPart(text),),
            created_at=now,
            agent=agent,
            model=model,
        )
        await self._store.add_session(session)
        await self._store.add_message(question)
        await self._engines[agent_config.engine].create(session.id)
        await self._start_turn(session, question, model, None)
        return TurnStarted(session.id, question)

    async def send_message(
        self,
        user: User,
        session_id: uuid.UUID,
        *,
        parent_id: uuid.UUID,
        model: str,
        text: str,
    ) -> TurnStarted:
        session = await self._store.get_session(session_id)
        messages = await self._store.messages_of(session_id)
        parent = next(m for m in messages if m.id == parent_id)
        question = Message(
            uuid.uuid4(),
            session_id,
            parent_id=parent_id,
            role=Role.USER,
            parts=(TextPart(text),),
            created_at=datetime.now(UTC),
            agent=session.agent,
            model=model,
        )
        await self._store.add_message(question)
        await self._start_turn(session, question, model, parent.checkpoint_id)
        return TurnStarted(session_id, question)

    async def regenerate_answer(
        self, user: User, session_id: uuid.UUID, *, question_id: uuid.UUID, model: str
    ) -> TurnStarted:
        session = await self._store.get_session(session_id)
        by_id = {m.id: m for m in await self._store.messages_of(session_id)}
        question = by_id[question_id]
        checkpoint_id = None
        if question.parent_id is not None:
            checkpoint_id = by_id[question.parent_id].checkpoint_id
        await self._start_turn(session, question, model, checkpoint_id)
        return TurnStarted(session_id, question)

    async def cancel_turn(self, user: User, session_id: uuid.UUID) -> None:
        self._turns[session_id].cancel()

    async def _start_turn(
        self, session: Session, question: Message, model: str, checkpoint_id: str | None
    ) -> None:
        agent_config = self._config.agents[session.agent]
        engine = self._engines[agent_config.engine]
        await self._store.start_turn(session.id, follows=question.id)
        self._turns[session.id] = asyncio.create_task(
            run_turn(self._store, engine, session, question, agent_config, model, checkpoint_id)
        )

    async def watch_turn(
        self, user: User, session_id: uuid.UUID, *, after: int = 0
    ) -> AsyncGenerator[NumberedEvent, None]:
        while True:
            for numbered in await self._store.events_after(session_id, after):
                yield numbered
                after = numbered.position
                if isinstance(numbered.event, TurnEnded):
                    return
            await self._store.wait_for_events(session_id, after)
            if (
                not await self._store.events_after(session_id, after)
                and await self._store.active_turn(session_id) is None
            ):
                return

    async def sweep(self) -> None:
        raise NotImplementedError("sweep")
