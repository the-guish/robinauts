# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The controller. An operation not yet implemented raises ``NotImplementedError``."""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import logging
import uuid
from collections.abc import AsyncGenerator, Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from robinauts.controller.application.intervals import run_at_intervals
from robinauts.controller.contract.broker.documents import (
    event_from_document,
    message_from_document,
    stored_message,
)
from robinauts.controller.contract.broker.domain import RUN_TURN, Task, TaskState
from robinauts.controller.contract.broker.partial import failed_answer, partial_answer
from robinauts.controller.contract.domain import (
    ActiveTurn,
    AgentListing,
    Identity,
    InvalidValueError,
    MessageNotFoundError,
    ModelListing,
    NoActiveTurnError,
    NumberedEvent,
    OpenedSession,
    SessionPage,
    UnknownModelError,
    User,
)
from robinauts.controller.contract.ports import Controller
from robinauts.controller.core.projects import project_name
from robinauts.controller.core.task_payloads import check_task_payload
from robinauts.controller.core.titles import title_from_text
from robinauts.controller.ports.store import Cursor, Store
from robinauts.shared_domain.domain import (
    DEFAULT_PROJECT,
    Config,
    Message,
    Project,
    Role,
    Session,
    TextPart,
    Turn,
    TurnEnded,
    TurnStarted,
    TurnState,
)

_log = logging.getLogger(__name__)

EVENT_WAIT_TIMEOUT = 15.0
"""How long a watcher waits for an event before it reads the store again, and checks whether
the turn's lease has passed."""

MAX_QUEUED_TIME = timedelta(hours=48)
"""How long a task may wait queued, written as its lease until a worker claims it: long enough
to outlast an outage of the workers."""

ENDED_BADLY = frozenset({TurnState.FAILED, TurnState.CANCELLED, TurnState.INTERRUPTED})
"""How a turn may end that opening its session says so."""


class RobinautsController(Controller):
    def __init__(
        self,
        config: Config,
        *,
        store: Store,
        event_wait_timeout: float = EVENT_WAIT_TIMEOUT,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._config = config
        self._store = store
        self._event_wait_timeout = event_wait_timeout
        self._now = now or (lambda: datetime.now(UTC))
        self._sweeping: asyncio.Task[None] | None = None

    async def open(self) -> None:
        await self._store.open()
        self._sweeping = asyncio.create_task(
            run_at_intervals(self._config.work.sweep_seconds, self.sweep), name="sweep"
        )

    async def close(self) -> None:
        sweeping, self._sweeping = self._sweeping, None
        if sweeping is not None:
            sweeping.cancel()
            await asyncio.gather(sweeping, return_exceptions=True)
        await self._store.close()

    async def ensure_user(self, identity: Identity) -> User:
        user = User(
            id=uuid.uuid4(),
            provider=identity.provider,
            subject=identity.subject,
            name=identity.name,
            email=identity.email,
            created_at=self._now(),
            hosted_domain=identity.hosted_domain,
            groups=identity.groups,
        )
        return await self._store.add_or_refresh_user(user)

    async def list_agents(self) -> tuple[AgentListing, ...]:
        return tuple(
            AgentListing(a.id, a.title, default_model=a.model, models=a.models)
            for a in self._config.agents.values()
        )

    async def list_models(self) -> tuple[ModelListing, ...]:
        return tuple(ModelListing(m.id, m.title or m.id) for m in self._config.models.values())

    async def list_sessions(
        self, user: User, *, limit: int, cursor: str | None = None
    ) -> SessionPage:
        before = None if cursor is None else _decode_cursor(cursor)
        sessions = await self._store.sessions_of(user.id, limit, before)
        following = _encode_cursor(sessions[-1]) if len(sessions) == limit else None
        running = await self._store.running_sessions(user.id, [s.id for s in sessions])
        return SessionPage(tuple(sessions), cursor=following, running=running)

    async def wait_for_changes(self, user: User, *, after: int | None, timeout: float) -> int:
        return await self._store.wait_for_changes(user.id, after, timeout)

    async def open_session(self, user: User, session_id: uuid.UUID) -> OpenedSession:
        session = await self._store.get_session(user.id, session_id)
        await self._end_expired(session)
        messages = await load_messages(self._store, user.id, session_id)
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

    async def archive_session(self, user: User, session_id: uuid.UUID) -> None:
        session = await self._store.get_session(user.id, session_id)
        if session.archived_at is None:
            await self._store.update_session(dataclasses.replace(session, archived_at=self._now()))

    async def delete_session(self, user: User, session_id: uuid.UUID) -> None:
        session = await self._store.get_session(user.id, session_id)
        await self._end_expired(session)
        running = await self._store.active_turn(user.id, session_id)
        if running is not None:
            await self._cancel(user, running)
        # A soft delete: the records and the engine's state stay. A turn another process
        # runs stops at its next write.
        await self._store.hide_session(user.id, session_id, self._now())

    async def fork_session(
        self, user: User, session_id: uuid.UUID, *, at_message: uuid.UUID
    ) -> Session:
        raise NotImplementedError("fork_session")

    async def list_projects(self, user: User) -> tuple[Project, ...]:
        await self._default_project(user)
        return tuple(await self._store.projects_of(user.id))

    async def create_project(self, user: User, name: str) -> Project:
        return await self._store.ensure_project(
            Project(uuid.uuid4(), user.id, project_name(name), self._now())
        )

    async def _default_project(self, user: User) -> Project:
        return await self._store.ensure_project(
            Project(uuid.uuid4(), user.id, DEFAULT_PROJECT, self._now())
        )

    async def start_session(
        self,
        user: User,
        *,
        agent: str,
        model: str,
        text: str,
        project: uuid.UUID | None = None,
        task_payload: Mapping[str, Any] | None = None,
    ) -> TurnStarted:
        payload = check_task_payload(task_payload)
        agent_config = self._config.agents[agent]
        # Before the session is stored, so that a refused model leaves no empty conversation.
        self._check_model(agent, model)
        if project is None:
            project = (await self._default_project(user)).id
        else:
            project = (await self._store.get_project(user.id, project)).id
        now = self._now()
        session = Session(
            uuid.uuid4(),
            owner_id=user.id,
            agent=agent,
            engine=agent_config.engine,
            created_at=now,
            updated_at=now,
            title=title_from_text(text),
            project_id=project,
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
            task_payload=payload,
        )
        await self._store.add_session(session)
        # The worker creates the engine's session before it runs this first turn.
        turn = await self._queue_turn(
            user, session, question, model, new_question=True, create_session=True
        )
        return TurnStarted(session.id, turn.id, question)

    async def send_message(
        self,
        user: User,
        session_id: uuid.UUID,
        *,
        parent_id: uuid.UUID,
        model: str,
        text: str,
        task_payload: Mapping[str, Any] | None = None,
    ) -> TurnStarted:
        payload = check_task_payload(task_payload)
        session = await self._store.get_session(user.id, session_id)
        messages = await load_messages(self._store, user.id, session_id)
        if all(m.id != parent_id for m in messages):
            raise MessageNotFoundError(str(parent_id))
        return await self._ask(user, session, parent_id, model, text, payload)

    async def edit_message(
        self,
        user: User,
        session_id: uuid.UUID,
        *,
        message_id: uuid.UUID,
        model: str,
        text: str,
        task_payload: Mapping[str, Any] | None = None,
    ) -> TurnStarted:
        payload = check_task_payload(task_payload)
        session = await self._store.get_session(user.id, session_id)
        messages = await load_messages(self._store, user.id, session_id)
        edited = next((m for m in messages if m.id == message_id), None)
        if edited is None or edited.role is not Role.USER:
            raise MessageNotFoundError(str(message_id))
        # An edit from the interface sends text alone: the task stays the edited question's.
        kept = edited.task_payload if payload is None else payload
        return await self._ask(user, session, edited.parent_id, model, text, kept)

    async def _ask(
        self,
        user: User,
        session: Session,
        parent_id: uuid.UUID | None,
        model: str,
        text: str,
        task_payload: Mapping[str, Any] | None = None,
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
            task_payload=task_payload,
        )
        turn = await self._queue_turn(user, session, question, model, new_question=True)
        return TurnStarted(session.id, turn.id, question)

    async def regenerate_answer(
        self, user: User, session_id: uuid.UUID, *, question_id: uuid.UUID, model: str
    ) -> TurnStarted:
        session = await self._store.get_session(user.id, session_id)
        question = next(
            (
                m
                for m in await load_messages(self._store, user.id, session_id)
                if m.id == question_id
            ),
            None,
        )
        if question is None:
            raise MessageNotFoundError(str(question_id))
        turn = await self._queue_turn(user, session, question, model, new_question=False)
        return TurnStarted(session_id, turn.id, question)

    async def retry_answer(
        self, user: User, session_id: uuid.UUID, *, answer_id: uuid.UUID, model: str
    ) -> TurnStarted:
        session = await self._store.get_session(user.id, session_id)
        by_id = {m.id: m for m in await load_messages(self._store, user.id, session_id)}
        failed = by_id.get(answer_id)
        if failed is None or not failed.failed or failed.parent_id is None:
            raise MessageNotFoundError(str(answer_id))
        question = by_id[failed.parent_id]
        turn = await self._queue_turn(
            user, session, question, model, new_question=False, retries_message_id=failed.id
        )
        return TurnStarted(session_id, turn.id, question)

    async def resume_answer(
        self, user: User, session_id: uuid.UUID, *, answer_id: uuid.UUID, model: str
    ) -> TurnStarted:
        session = await self._store.get_session(user.id, session_id)
        await self._end_expired(session)
        by_id = {m.id: m for m in await load_messages(self._store, user.id, session_id)}
        failed = by_id.get(answer_id)
        latest = await self._store.latest_turn(user.id, session_id)
        if (
            failed is None
            or not failed.failed
            or failed.parent_id is None
            or latest is None
            or failed.turn_id != latest.id
        ):
            raise MessageNotFoundError(str(answer_id))
        question = by_id[failed.parent_id]
        turn = await self._queue_turn(
            user, session, question, model, new_question=False, resume=True
        )
        return TurnStarted(session_id, turn.id, question)

    async def cancel_turn(
        self, user: User, session_id: uuid.UUID, turn_id: uuid.UUID | None = None
    ) -> None:
        await self._end_expired(await self._store.get_session(user.id, session_id))
        running = await self._store.active_turn(user.id, session_id)
        if running is None or (turn_id is not None and running.id != turn_id):
            raise NoActiveTurnError(str(session_id))
        await self._cancel(user, running)

    async def _cancel(self, user: User, turn: Turn) -> None:
        """Ask the turn to stop, wherever it runs: the store ends a turn whose task is queued,
        and announces a running one to the worker that runs it."""
        await self._store.request_cancel(user.id, turn.session_id, turn.id, self._now())

    def _check_model(self, agent: str, model: str) -> None:
        """``UnknownModelError`` for a model not configured, or not one the agent lists."""
        if model not in self._config.models:
            raise UnknownModelError(model)
        listed = self._config.agents[agent].models if agent in self._config.agents else ()
        if model not in listed:
            raise UnknownModelError(f"{model} is not one of agent {agent}'s models")

    async def _queue_turn(
        self,
        user: User,
        session: Session,
        question: Message,
        model: str,
        *,
        new_question: bool,
        retries_message_id: uuid.UUID | None = None,
        create_session: bool = False,
        resume: bool = False,
    ) -> Turn:
        self._check_model(session.agent, model)
        await self._end_expired(session)
        if session.archived_at is not None:
            await self._store.update_session(dataclasses.replace(session, archived_at=None))
        now = self._now()
        turn_id = uuid.uuid4()
        payload: dict[str, object] = {"turn_id": str(turn_id), "create_session": create_session}
        if resume:
            payload["resume"] = True
        task = Task(
            uuid.uuid4(),
            RUN_TURN,
            payload,
            TaskState.QUEUED,
            created_at=now,
            lease_until=now + MAX_QUEUED_TIME,
            worker=self._config.worker_of(session.agent),
        )
        turn = Turn(
            turn_id,
            session.id,
            follows=question.id,
            model=model,
            state=TurnState.ACTIVE,
            started_at=now,
            task_id=task.id,
            retries_message_id=retries_message_id,
        )
        stored = stored_message(question) if new_question else None
        # A worker claims the task and dispatches it.
        await self._store.queue_turn(user.id, turn, task, stored)
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
            await self._store.wait_for_events(
                user.id, session_id, turn.id, after, self._event_wait_timeout
            )
            if await self._store.events_after(user.id, session_id, turn.id, after):
                continue
            await self._end_expired(await self._store.get_session(user.id, session_id))
            current = await self._store.get_turn(user.id, session_id, turn.id)
            if current is None or current.state is not TurnState.ACTIVE:
                # Ended with no `turn_ended` event of its own, by a reader: the record says
                # how, under the last position stored.
                state = TurnState.INTERRUPTED if current is None else current.state
                yield NumberedEvent(after, TurnEnded(state))
                return

    async def sweep(self) -> None:
        now = self._now()
        for task in await self._store.expired_tasks(now):
            try:
                if task.name == RUN_TURN:
                    found = await self._store.find_turn(uuid.UUID(task.payload["turn_id"]))
                    if found is not None:
                        owner, turn = found
                        session = await self._store.get_session(owner, turn.session_id)
                        await self._end_expired(session, turn)
                        continue
                # No turn to end: a task of another kind, or of a deleted session.
                await self._store.end_expired_task(task.id, now)
            except Exception:
                _log.exception("could not end task %s, whose lease has passed", task.id)

    async def _end_expired(self, session: Session, turn: Turn | None = None) -> None:
        """End the session's active turn if its task's lease has passed, as ``interrupted``,
        and keep what it had streamed as its answer, marked failed. A runner gone with its
        process wrote no answer: the turn's events are what is left of it. A turn whose task
        waited too long queued has none."""
        now = self._now()
        if turn is None:
            turn = await self._store.active_turn(session.owner_id, session.id)
        if turn is None:
            return
        task = await self._store.get_task(turn.task_id)
        if task is None or task.state is TaskState.DONE or task.lease_until >= now:
            return
        events = await self._store.events_after(session.owner_id, session.id, turn.id, 0)
        answer_id, parts = partial_answer(document for _, document in events)
        answer = failed_answer(answer_id or uuid.uuid4(), session, turn, parts, now)
        stored = stored_message(answer)
        await self._store.end_expired_turn(session.owner_id, session.id, turn.id, now, stored)


async def load_messages(store: Store, owner: uuid.UUID, session: uuid.UUID) -> list[Message]:
    """Every message of the session, decoded, oldest first."""
    return [message_from_document(d) for d in await store.messages_of(owner, session)]


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
