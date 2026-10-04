# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The controller. An operation not yet implemented raises ``NotImplementedError``."""

from __future__ import annotations

import asyncio
import base64
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
from robinauts.controller.contract import logs
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
    Readiness,
    Role,
    Session,
    SessionNotFoundError,
    SessionPage,
    StorageConfig,
    StorageKind,
    Swept,
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
from robinauts.controller.contract.ports import Controller, Operations
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

LEASE_GRACE = 0.1
"""How long past a turn's lease its watcher looks again."""

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

SWEEP_SECONDS = 300.0
"""How often each process sweeps."""

SWEEP_BATCH = 1000
"""What one task of a sweep takes at a time."""

PURGE_AFTER = timedelta(minutes=10)
"""How long a session stays hidden before the sweep finishes its purge: a delete purges at
once, and one hidden for this long is one whose purge died."""

FINAL_WAIT = 10.0
"""How long `close` waits for the turns it interrupted to write their end, before it leaves
them to their leases: a database that does not answer must not hold the process."""

ENDED_BADLY = frozenset({TurnState.FAILED, TurnState.CANCELLED, TurnState.INTERRUPTED})
"""How a turn may end that opening its session says so."""


def _text(message: Message) -> str:
    return "".join(p.text for p in message.parts if isinstance(p, TextPart))


log = logging.getLogger(__name__)


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
        worker: str,
        sweep_every: float = SWEEP_SECONDS,
        close_timeout: float | None = None,
        final_wait: float = FINAL_WAIT,
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
        self._close_timeout = config.work.drain_seconds if close_timeout is None else close_timeout
        self._final_wait = final_wait
        self._draining = False
        self._housekeeper = Housekeeper(self.sweep, every=sweep_every)
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
        self._housekeeper.start()

    async def readiness(self) -> Readiness:
        problems = await self._store.readiness()
        if self._draining:
            problems = ("this process is stopping", *problems)
        return Readiness(tuple(problems))

    async def drain(self) -> None:
        if not self._draining:
            self._draining = True
            log.info("draining: no new turn is taken here")

    async def close(self) -> None:
        self._draining = True
        await self._housekeeper.stop()
        # The heartbeat goes on, and cancels still arrive, while the turns this process runs
        # are given their time; those still running then end as interrupted.
        await self._dispatcher.close(self._close_timeout, self._final_wait)
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
        # A conversation whose first turn never began has no message at all.
        thread = messages[-1:]
        while thread and thread[-1].parent_id is not None:
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
        # Everything that refuses a turn, before anything is stored: a refusal leaves no
        # conversation behind.
        if self._draining:
            raise DrainingError("this process is stopping; another one takes the turn")
        agent_config = self._config.agents.get(agent)
        if agent_config is None:
            raise UnknownAgentError(agent)
        if model not in self._config.models:
            raise UnknownModelError(model)
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
        engine = await self._engine(session.engine)
        await self._store.add_session(session)
        try:
            await engine.create(session.id)
            turn = await self._start_turn(user, session, question, model, new_question=True)
        except BaseException:
            # Refused after all, as by a pool with no connection free: the conversation the
            # turn would have begun goes with it.
            await engine.forget(session.id)
            await self._store.purge_session(user.id, session.id)
            raise
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
                ended = await self._store.end_expired_turn(
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
            if ended is not None:
                with logs.about(session=session.id, turn=turn.id):
                    log.warning(
                        "turn ended %s: its lease, held by %s, had passed",
                        state.value,
                        turn.worker_id,
                    )
            return ended
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
        if self._draining:
            raise DrainingError("this process is stopping; another one takes the turn")
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
        with logs.about(session=session_id, turn=turn_id):
            try:
                await self._run_turn(owner, session_id, turn_id, attempt)
            except asyncio.CancelledError as stopped:
                # Stopped before its runner claimed the turn, as by a cancel from another
                # process that arrived during the loading: the runner wrote nothing, so the
                # holder ends the turn here rather than leave it to its lease.
                state = None
                if StopReason.CANCEL in stopped.args:
                    state = TurnState.CANCELLED
                elif StopReason.CLOSE in stopped.args:
                    state = TurnState.INTERRUPTED
                if state is not None:
                    await self._end_unclaimed(owner, session_id, turn_id, attempt, state)
                raise

    async def _end_unclaimed(
        self,
        owner: uuid.UUID,
        session_id: uuid.UUID,
        turn_id: uuid.UUID,
        attempt: int,
        state: TurnState,
    ) -> None:
        """End the turn as ``state`` if it still runs, held here under ``attempt``, with no
        event of its runner's: nothing, when the runner ended it itself."""
        try:
            turn = await self._store.get_turn(owner, session_id, turn_id)
            if (
                turn is not None
                and turn.state is TurnState.RUNNING
                and (turn.worker_id, turn.attempt) == (self._worker, attempt)
                and await self._store.last_position(owner, session_id, turn_id) == 0
            ):
                await self._end_if_running(owner, turn, state)
        except Exception:
            log.exception("a turn stopped before its runner began could not be ended")

    async def _run_turn(
        self, owner: uuid.UUID, session_id: uuid.UUID, turn_id: uuid.UUID, attempt: int
    ) -> None:
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
            # No longer than the lease: a turn whose runner went away is ended, and its
            # watcher told, as soon as it may be, and not at the next timer.
            until_lease = (turn.lease_until - self._now()).total_seconds() + LEASE_GRACE
            wait = min(WAIT_SECONDS, max(LEASE_GRACE, until_lease))
            await self._store.wait_for_events(user.id, session_id, turn.id, after, wait)
            if await self._store.events_after(user.id, session_id, turn.id, after):
                continue
            await self._end_expired(user.id, session_id)
            current = await self._store.get_turn(user.id, session_id, turn.id)
            if current is not None:
                turn = current
            if current is None or current.state is not TurnState.RUNNING:
                if await self._store.events_after(user.id, session_id, turn.id, after):
                    continue
                # Ended with no `turn_ended` event of its own: the record says how, under the
                # last position stored.
                state = TurnState.INTERRUPTED if current is None else current.state
                yield NumberedEvent(after, TurnEnded(state))
                return

    async def sweep(self) -> Swept:
        now = self._now()
        skipped: list[str] = []
        deleted = await self._store.delete_expired(now, SWEEP_BATCH)
        if deleted is None:
            skipped.append("expired")
        ended = 0
        expired = await self._store.expired_turns(now, SWEEP_BATCH)
        if expired is None:
            skipped.append("turns")
        for owner, turn in expired or ():
            try:
                if await self._interrupt(owner, turn) is not None:
                    ended += 1
            except SessionNotFoundError:
                continue  # hidden meanwhile: the purge takes it
            except Exception:
                # One turn that cannot be ended must not stop the others, sweep after sweep.
                with logs.about(session=turn.session_id, turn=turn.id):
                    log.exception("the sweep could not end this turn; the next sweep tries")
        purged = 0
        hidden = await self._store.hidden_sessions(now - PURGE_AFTER, SWEEP_BATCH)
        if hidden is None:
            skipped.append("purge")
        for session in hidden or ():
            try:
                await (await self._engine(session.engine)).forget(session.id)
                await self._store.purge_session(session.owner_id, session.id)
                purged += 1
            except Exception:
                # As an engine this build no longer has: the others are purged all the same.
                with logs.about(session=session.id):
                    log.exception("the sweep could not purge this conversation")
        counts = deleted or {}
        return Swept(
            events=counts.get("turn_events", 0),
            user_sessions=counts.get("user_sessions", 0),
            api_tokens=counts.get("api_tokens", 0),
            pending_logins=counts.get("pending_logins", 0),
            turns_ended=ended,
            sessions_purged=purged,
            skipped=tuple(skipped),
        )


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
