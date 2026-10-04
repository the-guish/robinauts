# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The controller. An operation not yet implemented raises ``NotImplementedError``."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import dataclasses
import logging
import uuid
from collections.abc import AsyncGenerator, Callable
from datetime import UTC, datetime, timedelta

from robinauts.agent_engines.contract.ports import AgentEngine, EngineFactory, installed
from robinauts.controller.application.engines import build_engines
from robinauts.controller.application.housekeeping import Housekeeper
from robinauts.controller.application.turns import RETENTION, run_turn
from robinauts.controller.application.work import WorkLoop
from robinauts.controller.contract import context
from robinauts.controller.contract.domain import (
    ActiveTurn,
    AgentListing,
    Config,
    DrainingError,
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
    SessionNotFoundError,
    SessionPage,
    StorageConfig,
    StorageKind,
    TextPart,
    Turn,
    TurnEnded,
    TurnLostError,
    TurnStarted,
    TurnState,
    UnknownAgentError,
    UnknownEngineError,
    UnknownModelError,
    User,
)
from robinauts.controller.contract.ports import Controller, Credentials, Operations
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
from robinauts.controller.ports.dispatcher import CANCEL, TurnDispatcher
from robinauts.controller.ports.store import Cursor, Fence, Store, StoredEvent
from robinauts.controller.ports.work import WorkQueue

WAIT_SECONDS = 15.0
"""How long a watcher waits for an event before it reads the store again."""

CANCEL_WAIT = 5.0
"""How long a cancel, or the delete of a session whose turn runs, waits for the turn to end."""

LAST_POSITION = 2**31 - 1
"""A position past any event's."""

SWEEP_SECONDS = 300.0
"""How often every process sweeps."""

SWEEP_BATCH = 1000
"""How many rows one step of a sweep takes at most."""

SWEEP_BATCHES = 20
"""How many steps of deleting expired events one sweep takes at most."""

FINAL_WAIT = 10.0
"""How long `close` waits for anything after the turns: interrupted turns' last writes, and
the store letting its connections go. A database that does not answer must not hold a
stopping process until it is killed."""

READY_WAIT = 1.0
"""How long the readiness check waits for the database."""

ENDED_BADLY = frozenset({TurnState.FAILED, TurnState.CANCELLED, TurnState.INTERRUPTED})
"""How a turn may end that opening its session says so."""


log = logging.getLogger(__name__)


def _text(message: Message) -> str:
    return "".join(p.text for p in message.parts if isinstance(p, TextPart))


class RobinautsController(Controller, Operations):
    def __init__(
        self,
        config: Config,
        *,
        store: Store,
        storage: StorageConfig,
        secret_for: SecretLookup,
        dispatcher: TurnDispatcher,
        work: WorkQueue,
        worker_id: str,
        close_timeout: float | None = None,
        now: Callable[[], datetime] | None = None,
        sign_ins: Credentials | None = None,
        sweep_every: float = SWEEP_SECONDS,
    ) -> None:
        self._config = config
        self._store = store
        self._storage = storage
        self._secret_for = secret_for
        self._dispatcher = dispatcher
        self._close_timeout = config.work.drain_seconds if close_timeout is None else close_timeout
        self._draining = False
        self._now = now or (lambda: datetime.now(UTC))
        self._worker = worker_id
        self._sign_ins = sign_ins
        self._housekeeper = Housekeeper(self.sweep, every=sweep_every)
        self._work_loop = WorkLoop(
            work,
            dispatcher,
            worker_id,
            lease=timedelta(seconds=config.work.lease_seconds),
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
        self._housekeeper.start()

    async def close(self) -> None:
        """Give the turns running here their bounded window to finish, then end the rest as
        interrupted, and let everything go, each wait bounded."""
        self._draining = True
        await self._housekeeper.stop()
        await self._dispatcher.close(self._close_timeout)
        await self._work_loop.stop()
        self._engines = {}
        try:
            await asyncio.wait_for(self._store.close(), FINAL_WAIT)
        except TimeoutError:
            log.warning("the store did not close within %s s; stopping all the same", FINAL_WAIT)

    async def readiness(self) -> tuple[str, ...]:
        problems = ["this process is stopping"] if self._draining else []
        problems += await self._store.health(READY_WAIT)
        return tuple(problems)

    def drain(self) -> None:
        if not self._draining:
            log.info("draining: no new turn is taken here")
        self._draining = True

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
            position = await self._store.last_position(user.id, session_id, running.id)
            active = ActiveTurn(running.id, running.follows, position)
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
        ended = running is None or await self._cancel(user.id, session_id, running.id)
        # Hidden whatever runs: a turn that did not end in time writes nothing more, and its
        # engine's memory goes with the records once it has ended, by the sweep.
        await self._store.hide_session(user.id, session_id, self._now())
        if ended:
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
        return await self._cancel(user.id, session_id, running.id)

    async def _cancel(self, owner: uuid.UUID, session_id: uuid.UUID, turn_id: uuid.UUID) -> bool:
        """Ask for the turn's cancel, which reaches whichever process holds it, and wait up to
        ``CANCEL_WAIT`` for it to end: true when it has."""
        requested = await self._store.request_cancel(owner, session_id, turn_id, self._now())
        if requested is None:
            return True
        # This process's own is stopped here, without the signal's round trip; one whose
        # runner never started wrote nothing, and is ended here.
        if await self._dispatcher.stop(turn_id, CANCEL):
            await self._end_if_running(owner, requested, TurnState.CANCELLED)
        return await self._ended(owner, session_id, turn_id, CANCEL_WAIT)

    async def _ended(
        self, owner: uuid.UUID, session_id: uuid.UUID, turn_id: uuid.UUID, timeout: float
    ) -> bool:
        """Whether the turn has ended, or does within ``timeout`` seconds."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            current = await self._store.get_turn(owner, session_id, turn_id)
            if current is None or current.state is not TurnState.RUNNING:
                return True
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            # No event is past the last position there can be: this wakes at the end alone.
            await self._store.wait_for_events(owner, session_id, turn_id, LAST_POSITION, remaining)

    async def _end_if_running(self, owner: uuid.UUID, turn: Turn, state: TurnState) -> None:
        now = self._now()
        try:
            await self._store.finish_turn(
                owner,
                turn.session_id,
                turn.id,
                state,
                now,
                None,
                None,
                [],
                now,
                fence=_fence(turn),
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
        model_config = self._config.models.get(model)
        if model_config is None:
            raise UnknownModelError(model)
        if self._draining:
            raise DrainingError("this process is stopping: ask again")
        now = self._now()
        await self._end_expired(user.id, session.id)
        work = self._config.work
        # Held from the start, at attempt 1, by this process, which runs it.
        turn = Turn(
            uuid.uuid4(),
            session.id,
            follows=question.id,
            model=model,
            state=TurnState.RUNNING,
            started_at=now,
            lease_until=now + timedelta(seconds=work.lease_seconds),
            retries=retries,
            deadline_at=now + timedelta(seconds=work.max_turn_seconds),
            worker_id=self._worker,
            attempt=1,
            heartbeat_at=now,
        )
        stored = stored_message(question) if new_question else None
        await self._store.start_turn(user.id, turn, stored)
        await self._dispatcher.dispatch(user.id, session.id, turn.id, turn.attempt)
        return turn

    async def run_turn(self, owner: uuid.UUID, session_id: uuid.UUID, turn_id: uuid.UUID) -> None:
        """Run the turn, from its ids alone: what a worker in another process would call."""
        try:
            await self._run_turn(owner, session_id, turn_id)
        except asyncio.CancelledError as stopped:
            if CANCEL in stopped.args:
                # Cancelled before the runner had the turn in hand: it is ended here.
                with contextlib.suppress(SessionNotFoundError):
                    turn = await self._store.get_turn(owner, session_id, turn_id)
                    if turn is not None:
                        await self._end_if_running(owner, turn, TurnState.CANCELLED)
            raise

    async def _run_turn(self, owner: uuid.UUID, session_id: uuid.UUID, turn_id: uuid.UUID) -> None:
        context.about(session_id, turn_id)
        session = await self._store.get_session(owner, session_id)
        turn = await self._store.get_turn(owner, session_id, turn_id)
        if turn is None:
            return
        context.about(session_id, turn_id, turn.attempt)
        agent_config = self._config.agents.get(session.agent)
        model_config = self._config.models.get(turn.model)
        if agent_config is None or model_config is None:
            # A replica whose configuration is not the one that started the turn: the turn
            # ends at once, rather than holding its conversation until its lease passes.
            unknown = "agent" if agent_config is None else "model"
            name = session.agent if agent_config is None else turn.model
            await self._fail_unrunnable(owner, turn, f"{unknown} {name!r} is not configured here")
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
            question,
            prompt,
            agent_config,
            checkpoint_id,
            self._config.work.max_model_calls,
        )

    async def _end_expired(self, owner: uuid.UUID, session_id: uuid.UUID) -> Turn | None:
        """End the session's running turn as ``interrupted`` if its lease has passed: its
        runner went away. What it did, rebuilt from its events, is stored as a failed answer,
        so that the thread shows it and Retry starts it again."""
        running = await self._store.active_turn(owner, session_id)
        now = self._now()
        if running is None or running.lease_until >= now:
            return None
        session = await self._store.get_session(owner, session_id)
        events = await self._store.events_after(owner, session_id, running.id, 0)
        partial = partial_answer(event_from_document(d).event for _, d in events)
        answer = None
        # A turn whose cancel was asked for ends cancelled, and keeps nothing.
        if partial is not None and running.cancel_requested_at is None:
            answer = Message(
                partial.message_id,
                session_id,
                parent_id=partial.parent_id,
                role=Role.ASSISTANT,
                parts=partial.parts,
                created_at=now,
                agent=session.agent,
                engine=session.engine,
                model=running.model,
                turn_id=running.id,
                failed=True,
            )
        stored = None if answer is None else stored_message(answer)
        return await self._store.end_expired_turn(owner, session_id, running.id, now, stored)

    async def _fail_unrunnable(self, owner: uuid.UUID, turn: Turn, error: str) -> None:
        now = self._now()
        ended = event_to_document(turn.id, 1, TurnEnded(TurnState.FAILED))
        try:
            await self._store.finish_turn(
                owner,
                turn.session_id,
                turn.id,
                TurnState.FAILED,
                now,
                error,
                None,
                [StoredEvent(1, ended, now + RETENTION)],
                now,
                fence=_fence(turn),
            )
        except TurnLostError:
            return

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
                # Ended with no `turn_ended` event of its own, by a reader: the record says
                # how, under the last position stored.
                state = TurnState.INTERRUPTED if current is None else current.state
                yield NumberedEvent(after, TurnEnded(state))
                return

    async def sweep(self) -> None:
        now = self._now()
        # Turns whose runner went away: ended as their next reader would end them. First,
        # since their answers are rebuilt from events the next step may delete.
        for ref in await self._store.expired_turns(now, SWEEP_BATCH) or []:
            with contextlib.suppress(SessionNotFoundError):
                await self._end_expired(ref.owner, ref.session)
        for _ in range(SWEEP_BATCHES):
            deleted = await self._store.delete_expired_events(now, SWEEP_BATCH)
            if deleted is None or deleted < SWEEP_BATCH:
                break
        if self._sign_ins is not None:
            await self._sign_ins.delete_expired(now)
        # Deletes whose purge died, or waited for a turn to end.
        for ref in await self._store.purgeable_sessions(now, SWEEP_BATCH) or []:
            try:
                await (await self._engine(ref.engine)).forget(ref.session)
            except UnknownEngineError:
                log.exception("session %s is kept: its engine cannot forget it", ref.session)
                continue
            await self._store.purge_session(ref.owner, ref.session)


def _fence(turn: Turn) -> Fence:
    """The fence of the process holding the turn, as its record says."""
    return Fence(turn.worker_id or "", turn.attempt)


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
