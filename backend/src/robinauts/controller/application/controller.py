# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The controller. An operation not yet implemented raises ``NotImplementedError``."""

from __future__ import annotations

import asyncio
import dataclasses
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

from robinauts.agent_engines.contract.ports import AgentEngine, installed
from robinauts.controller.application.documents import (
    event_from_document,
    message_from_document,
    stored_message,
)
from robinauts.controller.application.engines import (
    SecretLookup,
    build_engines,
    engine_settings,
    engine_storage,
)
from robinauts.controller.application.turns import run_turn
from robinauts.controller.contract.domain import (
    ActiveTurn,
    AgentListing,
    Config,
    Identity,
    Message,
    MessageNotFoundError,
    ModelListing,
    NoActiveTurnError,
    NumberedEvent,
    OpenedSession,
    Role,
    Session,
    SessionPage,
    StorageConfig,
    TextPart,
    Turn,
    TurnEnded,
    TurnStarted,
    TurnState,
    UnknownModelError,
    User,
)
from robinauts.controller.contract.ports import Controller
from robinauts.controller.ports.store import Store

LEASE_MARGIN = timedelta(minutes=1)
"""What a turn's lease allows past its timeout."""

WAIT_SECONDS = 15.0
"""How long a watcher waits for an event before it reads the store again."""


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
        user = User(
            id=uuid.uuid4(),
            provider=identity.provider,
            subject=identity.subject,
            name=identity.name,
            email=identity.email,
            created_at=datetime.now(UTC),
        )
        return await self._store.add_user_if_absent(user)

    async def list_agents(self) -> tuple[AgentListing, ...]:
        return tuple(
            AgentListing(a.id, a.title, default_model=a.model) for a in self._config.agents.values()
        )

    async def list_models(self) -> tuple[ModelListing, ...]:
        return tuple(ModelListing(m.id, m.title or m.id) for m in self._config.models.values())

    async def list_sessions(
        self, user: User, *, limit: int, cursor: str | None = None
    ) -> SessionPage:
        sessions = await self._store.sessions_of(user.id, limit, None)
        return SessionPage(tuple(sessions), cursor=None)

    async def _messages(self, user: User, session_id: uuid.UUID) -> list[Message]:
        documents = await self._store.messages_of(user.id, session_id)
        return [message_from_document(d) for d in documents]

    async def open_session(self, user: User, session_id: uuid.UUID) -> OpenedSession:
        session = await self._store.get_session(user.id, session_id)
        messages = await self._messages(user, session_id)
        by_id = {m.id: m for m in messages}
        thread = [messages[-1]]
        while thread[-1].parent_id is not None:
            thread.append(by_id[thread[-1].parent_id])
        running = await self._store.active_turn(user.id, session_id)
        active = None
        if running is not None:
            events = await self._store.events_after(user.id, session_id, running.id, 0)
            active = ActiveTurn(running.id, running.follows, len(events))
        return OpenedSession(session, tuple(reversed(thread)), active)

    async def rename_session(self, user: User, session_id: uuid.UUID, title: str) -> Session:
        session = await self._store.get_session(user.id, session_id)
        renamed = dataclasses.replace(session, title=title, updated_at=datetime.now(UTC))
        await self._store.update_session(renamed)
        return renamed

    async def delete_session(self, user: User, session_id: uuid.UUID) -> None:
        session = await self._store.get_session(user.id, session_id)
        await self._store.hide_session(user.id, session_id, datetime.now(UTC))
        await self._engines[session.engine].forget(session_id)
        await self._store.purge_session(user.id, session_id)

    async def fork_session(
        self, user: User, session_id: uuid.UUID, *, at_message: uuid.UUID
    ) -> Session:
        raise NotImplementedError("fork_session")

    async def start_session(self, user: User, *, agent: str, model: str, text: str) -> TurnStarted:
        agent_config = self._config.agents[agent]
        now = datetime.now(UTC)
        session = Session(
            uuid.uuid4(),
            owner_id=user.id,
            agent=agent,
            engine=agent_config.engine,
            created_at=now,
            updated_at=now,
        )
        question = Message(
            uuid.uuid4(),
            session.id,
            parent_id=None,
            role=Role.USER,
            parts=(TextPart(text),),
            created_at=now,
            agent=agent,
            engine=session.engine,
            model=model,
        )
        await self._store.add_session(session)
        await self._engines[session.engine].create(session.id)
        turn = await self._start_turn(user, session, question, model, None, new_question=True)
        return TurnStarted(session.id, turn.id, question)

    async def send_message(
        self,
        user: User,
        session_id: uuid.UUID,
        *,
        parent_id: uuid.UUID,
        model: str,
        text: str,
    ) -> TurnStarted:
        session = await self._store.get_session(user.id, session_id)
        messages = await self._messages(user, session_id)
        parent = next((m for m in messages if m.id == parent_id), None)
        if parent is None:
            raise MessageNotFoundError(str(parent_id))
        question = Message(
            uuid.uuid4(),
            session_id,
            parent_id=parent_id,
            role=Role.USER,
            parts=(TextPart(text),),
            created_at=datetime.now(UTC),
            agent=session.agent,
            engine=session.engine,
            model=model,
        )
        turn = await self._start_turn(
            user, session, question, model, parent.checkpoint_id, new_question=True
        )
        return TurnStarted(session_id, turn.id, question)

    async def regenerate_answer(
        self, user: User, session_id: uuid.UUID, *, question_id: uuid.UUID, model: str
    ) -> TurnStarted:
        session = await self._store.get_session(user.id, session_id)
        by_id = {m.id: m for m in await self._messages(user, session_id)}
        question = by_id.get(question_id)
        if question is None:
            raise MessageNotFoundError(str(question_id))
        checkpoint_id = None
        if question.parent_id is not None:
            checkpoint_id = by_id[question.parent_id].checkpoint_id
        turn = await self._start_turn(
            user, session, question, model, checkpoint_id, new_question=False
        )
        return TurnStarted(session_id, turn.id, question)

    async def cancel_turn(
        self, user: User, session_id: uuid.UUID, turn_id: uuid.UUID | None = None
    ) -> None:
        self._turns[session_id].cancel()

    async def _start_turn(
        self,
        user: User,
        session: Session,
        question: Message,
        model: str,
        checkpoint_id: str | None,
        *,
        new_question: bool,
    ) -> Turn:
        agent_config = self._config.agents[session.agent]
        engine = self._engines[session.engine]
        model_config = self._config.models.get(model)
        if model_config is None:
            raise UnknownModelError(model)
        now = datetime.now(UTC)
        timeout = timedelta(seconds=model_config.timeout_seconds)
        turn = Turn(
            uuid.uuid4(),
            session.id,
            follows=question.id,
            model=model,
            state=TurnState.RUNNING,
            started_at=now,
            lease_until=now + timeout + LEASE_MARGIN,
        )
        stored = stored_message(question) if new_question else None
        await self._store.start_turn(user.id, turn, stored)
        self._turns[session.id] = asyncio.create_task(
            run_turn(
                self._store,
                engine,
                user.id,
                session,
                turn,
                question,
                agent_config,
                checkpoint_id,
                model_config.timeout_seconds,
            )
        )
        return turn

    async def watch_turn(
        self,
        user: User,
        session_id: uuid.UUID,
        turn_id: uuid.UUID | None = None,
        *,
        after: int = 0,
    ) -> AsyncGenerator[NumberedEvent, None]:
        if turn_id is None:
            turn = await self._store.latest_turn(user.id, session_id)
        else:
            turn = await self._store.get_turn(user.id, session_id, turn_id)
        if turn is None:
            raise NoActiveTurnError(str(session_id))
        while True:
            events = await self._store.events_after(user.id, session_id, turn.id, after)
            for _, document in events:
                numbered = event_from_document(document)
                yield numbered
                after = numbered.position
                if isinstance(numbered.event, TurnEnded):
                    return
            await self._store.wait_for_events(user.id, session_id, turn.id, after, WAIT_SECONDS)
            if not await self._store.events_after(user.id, session_id, turn.id, after):
                current = await self._store.get_turn(user.id, session_id, turn.id)
                if current is None or current.state is not TurnState.RUNNING:
                    return

    async def sweep(self) -> None:
        raise NotImplementedError("sweep")
