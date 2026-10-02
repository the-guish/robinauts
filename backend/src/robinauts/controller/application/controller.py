# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The controller. An operation not yet implemented raises ``NotImplementedError``."""

from __future__ import annotations

import base64
import binascii
import dataclasses
import uuid
from collections.abc import AsyncGenerator, Callable
from datetime import UTC, datetime, timedelta

from robinauts.agent_engines.contract.ports import AgentEngine, EngineFactory, installed
from robinauts.controller.application.engines import build_engines
from robinauts.controller.application.turns import run_turn
from robinauts.controller.contract.domain import (
    ActiveTurn,
    AgentListing,
    Config,
    Identity,
    InvalidValueError,
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
    StorageKind,
    TextPart,
    Turn,
    TurnActiveError,
    TurnEnded,
    TurnLostError,
    TurnStarted,
    TurnState,
    UnknownEngineError,
    UnknownModelError,
    User,
)
from robinauts.controller.contract.ports import Controller
from robinauts.controller.core.documents import (
    event_from_document,
    message_from_document,
    stored_message,
)
from robinauts.controller.core.engine_settings import (
    SecretLookup,
    engine_settings,
    engine_storage,
)
from robinauts.controller.core.titles import title_from_text
from robinauts.controller.ports.dispatcher import TurnDispatcher
from robinauts.controller.ports.store import Cursor, Store

LEASE_MARGIN = timedelta(minutes=1)
"""What a turn's lease allows past its timeout."""

WAIT_SECONDS = 15.0
"""How long a watcher waits for an event before it reads the store again."""

CLOSE_TIMEOUT = 10.0
"""How long `close` waits for the turns this process runs before it interrupts them."""


class RobinautsController(Controller):
    def __init__(
        self,
        config: Config,
        *,
        store: Store,
        storage: StorageConfig,
        secret_for: SecretLookup,
        dispatcher: TurnDispatcher,
        close_timeout: float = CLOSE_TIMEOUT,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._config = config
        self._store = store
        self._storage = storage
        self._secret_for = secret_for
        self._dispatcher = dispatcher
        self._close_timeout = close_timeout
        self._now = now or (lambda: datetime.now(UTC))
        self._engines: dict[str, AgentEngine] = {}
        self._factories: dict[str, EngineFactory] = {}
        self._handle: object | None = None

    def _sets_up_engines(self) -> bool:
        """On PostgreSQL `robinauts db init` set the engines up; the server never does."""
        return self._storage.kind is not StorageKind.POSTGRES

    async def open(self) -> None:
        self._handle = await self._store.open()
        self._factories = dict(installed())
        settings = engine_settings(self._config, self._secret_for)
        self._engines = await build_engines(
            self._config,
            settings,
            engine_storage(self._storage, self._handle),
            self._factories,
            setup=self._sets_up_engines(),
        )

    async def close(self) -> None:
        await self._dispatcher.close(self._close_timeout)
        self._engines = {}
        await self._store.close()

    async def _engine(self, name: str) -> AgentEngine:
        """The engine of that name: built at `open` for the agents, or on demand for a session
        whose engine the configuration no longer names."""
        engine = self._engines.get(name)
        if engine is None:
            factory = self._factories.get(name)
            if factory is None:
                raise UnknownEngineError(f"engine {name!r}, which this build does not have")
            settings = engine_settings(self._config, self._secret_for)
            engine = await factory(settings, engine_storage(self._storage, self._handle))
            if self._sets_up_engines():
                await engine.setup()
            self._engines[name] = engine
        return engine

    async def ensure_user(self, identity: Identity) -> User:
        user = User(
            id=uuid.uuid4(),
            provider=identity.provider,
            subject=identity.subject,
            name=identity.name,
            email=identity.email,
            created_at=self._now(),
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
        before = None if cursor is None else _decode_cursor(cursor)
        sessions = await self._store.sessions_of(user.id, limit, before)
        following = _encode_cursor(sessions[-1]) if len(sessions) == limit else None
        return SessionPage(tuple(sessions), cursor=following)

    async def _messages(self, owner: uuid.UUID, session_id: uuid.UUID) -> list[Message]:
        documents = await self._store.messages_of(owner, session_id)
        return [message_from_document(d) for d in documents]

    async def open_session(self, user: User, session_id: uuid.UUID) -> OpenedSession:
        session = await self._store.get_session(user.id, session_id)
        await self._store.end_expired_turn(user.id, session_id, self._now())
        messages = await self._messages(user.id, session_id)
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
        renamed = dataclasses.replace(session, title=title_from_text(title), updated_at=self._now())
        await self._store.update_session(renamed)
        return renamed

    async def delete_session(self, user: User, session_id: uuid.UUID) -> None:
        session = await self._store.get_session(user.id, session_id)
        await self._store.end_expired_turn(user.id, session_id, self._now())
        running = await self._store.active_turn(user.id, session_id)
        if running is not None and await self._dispatcher.cancel(user.id, session_id, running.id):
            await self._end_if_running(user, running, TurnState.CANCELLED)
        await self._store.hide_session(user.id, session_id, self._now())
        await (await self._engine(session.engine)).forget(session_id)
        await self._store.purge_session(user.id, session_id)

    async def fork_session(
        self, user: User, session_id: uuid.UUID, *, at_message: uuid.UUID
    ) -> Session:
        raise NotImplementedError("fork_session")

    async def start_session(self, user: User, *, agent: str, model: str, text: str) -> TurnStarted:
        agent_config = self._config.agents[agent]
        now = self._now()
        session = Session(
            uuid.uuid4(),
            owner_id=user.id,
            agent=agent,
            engine=agent_config.engine,
            created_at=now,
            updated_at=now,
            title=title_from_text(text),
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
        await (await self._engine(session.engine)).create(session.id)
        turn = await self._start_turn(user, session, question, model, new_question=True)
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
        messages = await self._messages(user.id, session_id)
        if all(m.id != parent_id for m in messages):
            raise MessageNotFoundError(str(parent_id))
        question = Message(
            uuid.uuid4(),
            session_id,
            parent_id=parent_id,
            role=Role.USER,
            parts=(TextPart(text),),
            created_at=self._now(),
            agent=session.agent,
            engine=session.engine,
            model=model,
        )
        turn = await self._start_turn(user, session, question, model, new_question=True)
        return TurnStarted(session_id, turn.id, question)

    async def regenerate_answer(
        self, user: User, session_id: uuid.UUID, *, question_id: uuid.UUID, model: str
    ) -> TurnStarted:
        session = await self._store.get_session(user.id, session_id)
        question = next(
            (m for m in await self._messages(user.id, session_id) if m.id == question_id), None
        )
        if question is None:
            raise MessageNotFoundError(str(question_id))
        turn = await self._start_turn(user, session, question, model, new_question=False)
        return TurnStarted(session_id, turn.id, question)

    async def cancel_turn(
        self, user: User, session_id: uuid.UUID, turn_id: uuid.UUID | None = None
    ) -> None:
        await self._store.get_session(user.id, session_id)
        await self._store.end_expired_turn(user.id, session_id, self._now())
        running = await self._store.active_turn(user.id, session_id)
        if running is None or (turn_id is not None and running.id != turn_id):
            raise NoActiveTurnError(str(session_id))
        if not await self._dispatcher.cancel(user.id, session_id, running.id):
            raise TurnActiveError(f"turn {running.id} runs in another process")
        # A runner that never claimed the turn wrote nothing: the turn is ended here.
        await self._end_if_running(user, running, TurnState.CANCELLED)

    async def _end_if_running(self, user: User, turn: Turn, state: TurnState) -> None:
        now = self._now()
        try:
            await self._store.finish_turn(
                user.id, turn.session_id, turn.id, state, now, None, None, [], now
            )
        except TurnLostError:
            return

    async def _start_turn(
        self, user: User, session: Session, question: Message, model: str, *, new_question: bool
    ) -> Turn:
        model_config = self._config.models.get(model)
        if model_config is None:
            raise UnknownModelError(model)
        now = self._now()
        await self._store.end_expired_turn(user.id, session.id, now)
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
        await self._dispatcher.dispatch(user.id, session.id, turn.id)
        return turn

    async def run_turn(self, owner: uuid.UUID, session_id: uuid.UUID, turn_id: uuid.UUID) -> None:
        """Run the turn, from its ids alone: what a worker in another process would call."""
        session = await self._store.get_session(owner, session_id)
        turn = await self._store.get_turn(owner, session_id, turn_id)
        if turn is None:
            return
        by_id = {m.id: m for m in await self._messages(owner, session_id)}
        question = by_id[turn.follows]
        checkpoint_id = None
        if question.parent_id is not None:
            checkpoint_id = by_id[question.parent_id].checkpoint_id
        agent_config = self._config.agents[session.agent]
        engine = await self._engine(session.engine)
        model_timeout = self._config.models[turn.model].timeout_seconds
        await run_turn(
            self._store,
            engine,
            owner,
            session,
            turn,
            question,
            agent_config,
            checkpoint_id,
            model_timeout,
        )

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
            if await self._store.events_after(user.id, session_id, turn.id, after):
                continue
            await self._store.end_expired_turn(user.id, session_id, self._now())
            current = await self._store.get_turn(user.id, session_id, turn.id)
            if current is None or current.state is not TurnState.RUNNING:
                # Ended with no `turn_ended` event of its own, by a reader: the record says
                # how, under the last position stored.
                state = TurnState.INTERRUPTED if current is None else current.state
                yield NumberedEvent(after, TurnEnded(state))
                return

    async def sweep(self) -> None:
        raise NotImplementedError("sweep")


def _encode_cursor(session: Session) -> str:
    """Where a page ended, as an opaque string: the last session's ``(updated_at, id)``."""
    plain = f"{session.updated_at.isoformat()} {session.id}"
    return base64.urlsafe_b64encode(plain.encode()).decode()


def _decode_cursor(cursor: str) -> Cursor:
    try:
        when, which = base64.urlsafe_b64decode(cursor.encode()).decode().split(" ")
        updated_at = datetime.fromisoformat(when)
        if updated_at.tzinfo is None:
            raise ValueError("a time without its zone")
        return updated_at, uuid.UUID(which)
    except (ValueError, UnicodeDecodeError, binascii.Error) as exc:
        raise InvalidValueError(f"a cursor that does not decode: {cursor!r}") from exc
