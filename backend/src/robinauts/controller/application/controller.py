# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The controller. An operation not yet implemented raises ``NotImplementedError``."""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import uuid
from collections.abc import AsyncGenerator, Callable
from datetime import UTC, datetime, timedelta

from robinauts.agent_engines.contract.ports import AgentEngine, EngineFactory, installed
from robinauts.controller.application.engines import build_engines
from robinauts.controller.application.turns import RETENTION, run_turn
from robinauts.controller.application.work import WorkLoop
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
    UnknownAgentError,
    UnknownEngineError,
    UnknownModelError,
    User,
)
from robinauts.controller.contract.ports import Controller
from robinauts.controller.core.documents import (
    event_from_document,
    event_to_document,
    message_from_document,
    stored_message,
)
from robinauts.controller.core.engine_settings import (
    SecretLookup,
    engine_settings,
    engine_storage,
)
from robinauts.controller.core.failures import prompt_after_failures
from robinauts.controller.core.titles import title_from_text
from robinauts.controller.core.transcript import partial_answer
from robinauts.controller.ports.dispatcher import StopReason, TurnDispatcher
from robinauts.controller.ports.store import Cursor, Store, StoredEvent
from robinauts.controller.ports.work import Fence, WorkQueue

WAIT_SECONDS = 15.0
"""How long a watcher waits for an event before it reads the store again."""

LEASE_EXPIRED = "lease expired"
"""The error of a turn ended because its runner went away, for the operator."""

INTERRUPT_TRIES = 3
"""How often ending an expired turn reads its events again, when a write of its runner's,
whose clock is behind, landed meanwhile."""

CANCEL_WAIT = 5.0
"""How long a cancel waits for the turn to end, wherever it runs, before it answers that the
end is on its way."""

DELETE_WAIT = 10.0
"""How long a delete waits for the session's turn to end before it is refused."""

NO_POSITION = 2**31 - 1
"""A position past any event: waiting after it wakes on the turn's end alone."""

CLOSE_TIMEOUT = 10.0
"""How long `close` waits for the turns this process runs before it interrupts them."""

ENDED_BADLY = frozenset({TurnState.FAILED, TurnState.CANCELLED, TurnState.INTERRUPTED})
"""How a turn may end that opening its session says so."""


def _text(message: Message) -> str:
    return "".join(p.text for p in message.parts if isinstance(p, TextPart))


class RobinautsController(Controller):
    def __init__(
        self,
        config: Config,
        *,
        store: Store,
        storage: StorageConfig,
        secret_for: SecretLookup,
        dispatcher: TurnDispatcher,
        work: WorkQueue,
        worker: str,
        close_timeout: float = CLOSE_TIMEOUT,
        cancel_wait: float = CANCEL_WAIT,
        delete_wait: float = DELETE_WAIT,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._config = config
        self._store = store
        self._storage = storage
        self._secret_for = secret_for
        self._dispatcher = dispatcher
        self._worker = worker
        self._close_timeout = close_timeout
        self._cancel_wait = cancel_wait
        self._delete_wait = delete_wait
        self._now = now or (lambda: datetime.now(UTC))
        self._lease = timedelta(seconds=config.work.lease_seconds)
        self._work_loop = WorkLoop(
            work,
            dispatcher,
            worker,
            lease=self._lease,
            every=config.work.heartbeat_seconds,
            now=lambda: self._now(),
        )
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
        self._work_loop.start()

    async def close(self) -> None:
        # The heartbeat goes on while the turns this process runs are given their time.
        await self._dispatcher.close(self._close_timeout)
        await self._work_loop.stop()
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
            engine = factory(settings, engine_storage(self._storage, self._handle))
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
        await self._end_expired(user.id, session_id)
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
        latest = await self._store.latest_turn(user.id, session_id)
        ended_badly = None
        if latest is not None and latest.state in ENDED_BADLY:
            ended_badly = latest
        return OpenedSession(session, tuple(reversed(thread)), active, ended_badly)

    async def rename_session(self, user: User, session_id: uuid.UUID, title: str) -> Session:
        session = await self._store.get_session(user.id, session_id)
        renamed = dataclasses.replace(session, title=title_from_text(title), updated_at=self._now())
        await self._store.update_session(renamed)
        return renamed

    async def delete_session(self, user: User, session_id: uuid.UUID) -> None:
        session = await self._store.get_session(user.id, session_id)
        await self._end_expired(user.id, session_id)
        running = await self._store.active_turn(user.id, session_id)
        # Whichever process runs it: no runner and no engine may write under a purge.
        if running is not None and not await self._cancel(user.id, running, self._delete_wait):
            raise TurnActiveError(f"turn {running.id} has not stopped yet")
        await self._store.hide_session(user.id, session_id, self._now())
        await (await self._engine(session.engine)).forget(session_id)
        await self._store.purge_session(user.id, session_id)

    async def fork_session(
        self, user: User, session_id: uuid.UUID, *, at_message: uuid.UUID
    ) -> Session:
        raise NotImplementedError("fork_session")

    async def start_session(self, user: User, *, agent: str, model: str, text: str) -> TurnStarted:
        agent_config = self._config.agents.get(agent)
        if agent_config is None:
            raise UnknownAgentError(agent)
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
        return await self._ask(user, session, parent_id, model, text)

    async def edit_message(
        self,
        user: User,
        session_id: uuid.UUID,
        *,
        message_id: uuid.UUID,
        model: str,
        text: str,
    ) -> TurnStarted:
        session = await self._store.get_session(user.id, session_id)
        messages = await self._messages(user.id, session_id)
        edited = next((m for m in messages if m.id == message_id), None)
        if edited is None or edited.role is not Role.USER:
            raise MessageNotFoundError(str(message_id))
        return await self._ask(user, session, edited.parent_id, model, text)

    async def _ask(
        self, user: User, session: Session, parent_id: uuid.UUID | None, model: str, text: str
    ) -> TurnStarted:
        if not text or not text.strip():
            raise InvalidValueError("a message needs some text")
        question = Message(
            uuid.uuid4(),
            session.id,
            parent_id=parent_id,
            role=Role.USER,
            parts=(TextPart(text),),
            created_at=self._now(),
            agent=session.agent,
            engine=session.engine,
            model=model,
        )
        turn = await self._start_turn(user, session, question, model, new_question=True)
        return TurnStarted(session.id, turn.id, question)

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

    async def retry_answer(
        self, user: User, session_id: uuid.UUID, *, answer_id: uuid.UUID, model: str
    ) -> TurnStarted:
        session = await self._store.get_session(user.id, session_id)
        by_id = {m.id: m for m in await self._messages(user.id, session_id)}
        failed = by_id.get(answer_id)
        if failed is None or not failed.failed or failed.parent_id is None:
            raise MessageNotFoundError(str(answer_id))
        question = by_id[failed.parent_id]
        turn = await self._start_turn(
            user, session, question, model, new_question=False, retries=failed.id
        )
        return TurnStarted(session_id, turn.id, question)

    async def cancel_turn(
        self, user: User, session_id: uuid.UUID, turn_id: uuid.UUID | None = None
    ) -> bool:
        await self._store.get_session(user.id, session_id)
        await self._end_expired(user.id, session_id)
        running = await self._store.active_turn(user.id, session_id)
        if running is None or (turn_id is not None and running.id != turn_id):
            raise NoActiveTurnError(str(session_id))
        return await self._cancel(user.id, running, self._cancel_wait)

    async def _cancel(self, owner: uuid.UUID, turn: Turn, wait: float) -> bool:
        """Ask for the turn to stop through the store, so that its holder stops it wherever it
        runs, and wait up to ``wait`` seconds for it to end: true when it did."""
        asked = await self._store.request_cancel(owner, turn.session_id, turn.id, self._now())
        if asked is None or asked.state is not TurnState.RUNNING:
            return True
        if await self._dispatcher.stop(turn.id, StopReason.CANCEL):
            # A runner stopped before it began wrote nothing: its holder ends the turn here.
            await self._end_if_running(owner, asked, TurnState.CANCELLED)
            return True
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait
        while True:
            await self._end_expired(owner, turn.session_id)
            current = await self._store.get_turn(owner, turn.session_id, turn.id)
            if current is None or current.state is not TurnState.RUNNING:
                return True
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            await self._store.wait_for_events(
                owner, turn.session_id, turn.id, NO_POSITION, remaining
            )

    async def _end_expired(self, owner: uuid.UUID, session_id: uuid.UUID) -> None:
        """End the session's running turn if its lease has passed: its runner went away."""
        running = await self._store.active_turn(owner, session_id)
        if running is not None and running.lease_until < self._now():
            await self._interrupt(owner, running)

    async def _interrupt(self, owner: uuid.UUID, turn: Turn) -> Turn | None:
        """End a turn whose lease has passed as ``interrupted``, storing what it had answered,
        read back from its events, as an answer that failed: the thread shows what it did,
        and Retry starts it over. A turn whose cancel was asked for ends ``cancelled``, with
        no answer, as its runner would have ended it. ``None`` when it was not such a turn
        after all."""
        session = await self._store.get_session(owner, turn.session_id)
        state = TurnState.INTERRUPTED
        if turn.cancel_requested_at is not None:
            state = TurnState.CANCELLED
        for _ in range(INTERRUPT_TRIES):
            now = self._now()
            stored = await self._store.events_after(owner, session.id, turn.id, 0)
            numbered = [event_from_document(document) for _, document in stored]
            partial = partial_answer(numbered)
            answer = None
            if partial is not None and state is TurnState.INTERRUPTED:
                answer = Message(
                    partial.message_id,
                    session.id,
                    parent_id=partial.parent_id,
                    role=Role.ASSISTANT,
                    parts=partial.parts,
                    created_at=now,
                    agent=session.agent,
                    engine=session.engine,
                    model=turn.model,
                    turn_id=turn.id,
                    failed=True,
                )
            position = max((n.position for n in numbered), default=0) + 1
            ended = TurnEnded(state)
            last = StoredEvent(
                position, event_to_document(turn.id, position, ended), now + RETENTION
            )
            try:
                return await self._store.end_expired_turn(
                    owner,
                    session.id,
                    turn.id,
                    state,
                    now,
                    LEASE_EXPIRED if state is TurnState.INTERRUPTED else None,
                    None if answer is None else stored_message(answer),
                    [last],
                )
            except TurnLostError:
                continue
        return None

    async def _end_if_running(self, owner: uuid.UUID, turn: Turn, state: TurnState) -> None:
        now = self._now()
        try:
            await self._store.finish_turn(
                owner,
                turn.session_id,
                turn.id,
                Fence(self._worker, turn.attempt),
                state,
                now,
                None,
                None,
                [],
                now,
            )
        except TurnLostError:
            return

    async def _start_turn(
        self,
        user: User,
        session: Session,
        question: Message,
        model: str,
        *,
        new_question: bool,
        retries: uuid.UUID | None = None,
    ) -> Turn:
        if session.agent not in self._config.agents:
            raise UnknownAgentError(session.agent)
        if model not in self._config.models:
            raise UnknownModelError(model)
        await self._end_expired(user.id, session.id)
        now = self._now()
        deadline = now + timedelta(seconds=self._config.work.max_turn_seconds)
        turn = Turn(
            uuid.uuid4(),
            session.id,
            follows=question.id,
            model=model,
            state=TurnState.RUNNING,
            started_at=now,
            lease_until=now + self._lease,
            retries=retries,
            deadline_at=deadline,
            worker_id=self._worker,
            attempt=1,
            heartbeat_at=now,
        )
        stored = stored_message(question) if new_question else None
        await self._store.start_turn(user.id, turn, stored)
        await self._dispatcher.dispatch(user.id, session.id, turn.id, turn.attempt)
        return turn

    async def run_turn(
        self, owner: uuid.UUID, session_id: uuid.UUID, turn_id: uuid.UUID, attempt: int
    ) -> None:
        """Run the turn's attempt, from its ids alone: what a worker in another process would
        call. A turn this process does not hold under that attempt is not run."""
        session = await self._store.get_session(owner, session_id)
        turn = await self._store.get_turn(owner, session_id, turn_id)
        if turn is None or (turn.worker_id, turn.attempt) != (self._worker, attempt):
            return
        by_id = {m.id: m for m in await self._messages(owner, session_id)}
        question = by_id[turn.follows]
        # The nearest answer up the thread that has a checkpoint: a failed answer has
        # none, and continuing from nothing would start the engine's memory again.
        checkpoint_id = None
        above = question.parent_id
        while above is not None and checkpoint_id is None:
            checkpoint_id = by_id[above].checkpoint_id
            above = by_id[above].parent_id
        agent_config = self._config.agents[session.agent]
        engine = await self._engine(session.engine)
        # The failed exchanges between the last answer that finished and this question,
        # oldest first: the engine remembers none of them, so the prompt carries them.
        earlier: list[tuple[str, Message]] = []
        above = question.parent_id
        while above is not None and by_id[above].failed:
            failed = by_id[above]
            asked = by_id[failed.parent_id] if failed.parent_id is not None else None
            if asked is None:
                break
            earlier.insert(0, (_text(asked), failed))
            above = asked.parent_id
        retried = None if turn.retries is None else by_id[turn.retries]
        prompt = prompt_after_failures(_text(question), earlier, retried)
        await run_turn(
            self._store,
            engine,
            owner,
            session,
            turn,
            Fence(self._worker, attempt),
            question,
            prompt,
            agent_config,
            checkpoint_id,
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
            await self._end_expired(user.id, session_id)
            current = await self._store.get_turn(user.id, session_id, turn.id)
            if current is None or current.state is not TurnState.RUNNING:
                if await self._store.events_after(user.id, session_id, turn.id, after):
                    continue
                # Ended with no `turn_ended` event of its own: the record says how, under the
                # last position stored.
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
    except ValueError as exc:  # UnicodeDecodeError and binascii.Error are ValueErrors
        raise InvalidValueError(f"a cursor that does not decode: {cursor!r}") from exc
