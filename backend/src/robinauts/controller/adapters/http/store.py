# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The store over the worker API: what a sandbox worker runs its turns on.

The worker of ``controller.application.worker`` runs here as it runs in the server, on the few
operations it calls, each one call of the worker API (``docs/architecture/sandbox-workers.md``).
Every request carries the workers' token and the id this process minted when it started, which
is the sandbox. Every other operation of the port is the server's, and refused here.

- **A claim** is ``GET /task``, which ``wait_for_queued`` holds open as a long-poll. It keeps
  the task it brings back for the ``claim_tasks`` that follows, which asks nothing itself.
- **Loading a turn** is ``GET /turns/{turn}``, which brings the session and its messages too:
  ``get_session`` and ``messages_of`` answer from it, for the session whose turn is running.
- **Events** and **the finish** are ``POST``s, and **the leases** one ``POST`` for every task
  this process runs, whose answer names the tasks asked to stop. A cancel reaches this process
  that way alone, at the next heartbeat.

The owner of a session is never sent to a sandbox: it is ``NO_OWNER`` here, and the server
takes the real one from the session. A transport failure, or a server that answers 502, 503 or
504, is retried with backoff for up to ``RETRY_SECONDS``, so a server restarting costs a pause
and not a turn: an append retried is the same document at the same position, which the server
accepts again.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any

import httpx

from robinauts.controller.contract.domain import (
    InvalidValueError,
    LoadedTurn,
    Session,
    SessionNotFoundError,
    Task,
    Turn,
    TurnLostError,
    TurnState,
    User,
    WorkerRefusedError,
)
from robinauts.controller.contract.worker_wire import (
    NO_OWNER,
    PREFIX,
    SANDBOX_HEADER,
    loaded_turn_from_wire,
    task_from_wire,
)
from robinauts.controller.ports.store import Cursor, Document, Store, StoredEvent, StoredMessage

_log = logging.getLogger(__name__)

RETRY_SECONDS = 60.0
"""How long a call is retried through a server that cannot be reached, before it fails."""

FIRST_PAUSE = 0.5
LAST_PAUSE = 8.0

REQUEST_SECONDS = 30.0
"""How long one request may take, past the wait a claim asks for."""

RETRIED_STATUSES = frozenset({502, 503, 504})


class WorkerApiRefusedError(RuntimeError):
    """The server refused the workers' token, or answered something this build cannot read."""


class HttpStore(Store):
    def __init__(
        self,
        api_url: str,
        token: str,
        *,
        sandbox: uuid.UUID | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        retry_seconds: float = RETRY_SECONDS,
    ) -> None:
        """``transport`` is for a test, which serves the API in its own process."""
        self.sandbox = sandbox or uuid.uuid4()
        self._api_url = api_url.rstrip("/")
        self._token = token
        self._transport = transport
        self._retry_seconds = retry_seconds
        self._client: httpx.AsyncClient | None = None
        self._claimed: list[Task] = []
        """Tasks a wait brought back, for the next claim."""
        self._tasks: dict[uuid.UUID, Task] = {}
        """The tasks this process claimed and has not finished, by id."""
        self._loaded: dict[uuid.UUID, LoadedTurn] = {}
        """The turn last loaded of each session with a turn running here, by session id."""

    # --- the connection --------------------------------------------------------------

    async def open(self) -> None:
        """Nothing for the engines: their memory is this process's."""
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._api_url + PREFIX,
                headers={
                    "Authorization": f"Bearer {self._token}",
                    SANDBOX_HEADER: str(self.sandbox),
                },
                timeout=REQUEST_SECONDS,
                transport=self._transport,
            )

    async def close(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()

    async def _call(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
        timeout: float = REQUEST_SECONDS,
    ) -> httpx.Response:
        """The answer of the server, retried through what a restart looks like; a refusal is
        raised as the store's error it stands for."""
        if self._client is None:
            raise RuntimeError("the store is not open")
        loop = asyncio.get_running_loop()
        give_up = loop.time() + self._retry_seconds
        pause = FIRST_PAUSE
        while True:
            try:
                answer = await self._client.request(
                    method, path, json=json, params=params, timeout=timeout
                )
            except httpx.TransportError as failed:
                trouble: str = f"{type(failed).__name__}: {failed}"
            else:
                if answer.status_code not in RETRIED_STATUSES:
                    return _checked(answer)
                trouble = f"status {answer.status_code}"
            if loop.time() + pause > give_up:
                raise ConnectionError(f"the worker API did not answer {method} {path}: {trouble}")
            _log.warning("the worker API did not answer (%s): trying again in %s s", trouble, pause)
            await asyncio.sleep(pause)
            pause = min(pause * 2, LAST_PAUSE)

    # --- what the worker calls -------------------------------------------------------

    async def claim_tasks(
        self, now: datetime, until: datetime, limit: int, sandbox: uuid.UUID | None = None
    ) -> list[Task]:
        """The tasks a wait brought back: the claim is the wait's, a long-poll, so that an
        idle worker asks once per poll and not twice. The server dates the claim and the
        lease; ``now`` and ``until`` are not sent."""
        taken, self._claimed = self._claimed[:limit], self._claimed[limit:]
        return taken

    async def wait_for_queued(
        self, now: datetime, timeout: float, sandbox: uuid.UUID | None = None
    ) -> bool:
        if self._claimed:
            return True
        task = await self._claim(timeout)
        if task is None:
            return False
        self._claimed.append(task)
        return True

    async def _claim(self, wait: float) -> Task | None:
        answer = await self._call(
            "GET", "/task", params={"wait": f"{wait:g}"}, timeout=wait + REQUEST_SECONDS
        )
        if answer.status_code == 204:
            return None
        task = task_from_wire(_body(answer).get("task"))
        self._tasks[task.id] = task
        return task

    async def get_task(self, task: uuid.UUID) -> Task | None:
        return self._tasks.get(task)

    async def find_turn(self, turn: uuid.UUID) -> tuple[uuid.UUID, Turn] | None:
        try:
            answer = await self._call("GET", f"/turns/{turn}")
        except SessionNotFoundError:
            return None
        loaded = loaded_turn_from_wire(_body(answer))
        self._loaded[loaded.session.id] = loaded
        return NO_OWNER, loaded.turn

    async def get_session(self, owner: uuid.UUID, session: uuid.UUID) -> Session:
        return self._loaded_of(session).session

    async def messages_of(self, owner: uuid.UUID, session: uuid.UUID) -> list[Document]:
        return list(self._loaded_of(session).messages)

    def _loaded_of(self, session: uuid.UUID) -> LoadedTurn:
        loaded = self._loaded.get(session)
        if loaded is None:
            raise SessionNotFoundError(f"no turn of session {session} is running here")
        return loaded

    async def append_event(
        self,
        owner: uuid.UUID,
        session: uuid.UUID,
        turn: uuid.UUID,
        position: int,
        document: Document,
        written_at: datetime,
        expires_at: datetime,
    ) -> None:
        await self._call("POST", f"/turns/{turn}/events", json={"events": [dict(document)]})

    async def finish_turn(
        self,
        owner: uuid.UUID,
        session: uuid.UUID,
        turn: uuid.UUID,
        state: TurnState,
        ended_at: datetime,
        error: str | None,
        answer: StoredMessage | None,
        events: Sequence[StoredEvent],
        updated_at: datetime,
    ) -> None:
        body = {
            "state": state.value,
            "error": error,
            "answer": None if answer is None else dict(answer.document),
            "events": [dict(event.document) for event in events],
        }
        try:
            await self._call("POST", f"/turns/{turn}/finish", json=body)
        finally:
            loaded = self._loaded.get(session)
            if loaded is not None and loaded.turn.id == turn:
                del self._loaded[session]
                self._tasks.pop(loaded.turn.task_id, None)

    async def renew_leases(
        self, tasks: Sequence[uuid.UUID], now: datetime, until: datetime
    ) -> list[uuid.UUID]:
        answer = await self._call("POST", "/lease", json={"tasks": [str(t) for t in tasks]})
        stop = _body(answer).get("cancel", [])
        if not isinstance(stop, list):
            raise WorkerApiRefusedError("a lease's answer whose cancel is not a list")
        return [uuid.UUID(str(task)) for task in stop]

    async def listen_for_cancels(self, stop: Callable[[uuid.UUID], object]) -> None:
        """Nothing to listen to: a stop comes back with the next renewal of the leases."""

    # --- the server's alone ----------------------------------------------------------

    async def add_or_refresh_user(self, user: User) -> User:
        raise _not_here("add_or_refresh_user")

    async def add_session(self, session: Session) -> None:
        raise _not_here("add_session")

    async def update_session(self, session: Session) -> None:
        raise _not_here("update_session")

    async def sessions_of(
        self, owner: uuid.UUID, limit: int, before: Cursor | None
    ) -> list[Session]:
        raise _not_here("sessions_of")

    async def hide_session(self, owner: uuid.UUID, session: uuid.UUID, at: datetime) -> None:
        raise _not_here("hide_session")

    async def purge_session(self, owner: uuid.UUID, session: uuid.UUID, now: datetime) -> bool:
        raise _not_here("purge_session")

    async def queue_turn(
        self, owner: uuid.UUID, turn: Turn, task: Task, question: StoredMessage | None
    ) -> None:
        raise _not_here("queue_turn")

    async def events_after(
        self, owner: uuid.UUID, session: uuid.UUID, turn: uuid.UUID, position: int
    ) -> list[tuple[int, Document]]:
        raise _not_here("events_after")

    async def end_expired_turn(
        self,
        owner: uuid.UUID,
        session: uuid.UUID,
        turn: uuid.UUID,
        now: datetime,
        answer: StoredMessage | None = None,
    ) -> Turn | None:
        raise _not_here("end_expired_turn")

    async def request_cancel(
        self, owner: uuid.UUID, session: uuid.UUID, turn: uuid.UUID, at: datetime
    ) -> None:
        raise _not_here("request_cancel")

    async def hidden_sessions(self, now: datetime) -> list[Session]:
        raise _not_here("hidden_sessions")

    async def expired_tasks(self, now: datetime) -> list[Task]:
        raise _not_here("expired_tasks")

    async def end_expired_task(self, task: uuid.UUID, now: datetime) -> bool:
        raise _not_here("end_expired_task")

    async def active_turn(self, owner: uuid.UUID, session: uuid.UUID) -> Turn | None:
        raise _not_here("active_turn")

    async def latest_turn(self, owner: uuid.UUID, session: uuid.UUID) -> Turn | None:
        raise _not_here("latest_turn")

    async def get_turn(self, owner: uuid.UUID, session: uuid.UUID, turn: uuid.UUID) -> Turn | None:
        raise _not_here("get_turn")

    async def wait_for_events(
        self, owner: uuid.UUID, session: uuid.UUID, turn: uuid.UUID, after: int, timeout: float
    ) -> bool:
        raise _not_here("wait_for_events")

    async def hear_from_sandbox(self, sandbox: uuid.UUID, now: datetime) -> None:
        raise _not_here("hear_from_sandbox")

    async def sandbox_heard_at(self, sandbox: uuid.UUID) -> datetime | None:
        raise _not_here("sandbox_heard_at")


def _not_here(operation: str) -> NotImplementedError:
    return NotImplementedError(f"{operation}: the server's, not the worker API's")


def _body(answer: httpx.Response) -> dict[str, Any]:
    try:
        body = answer.json()
    except ValueError as exc:
        raise WorkerApiRefusedError(f"an answer that is not JSON: {answer.text[:200]!r}") from exc
    if not isinstance(body, dict):
        raise WorkerApiRefusedError("an answer that is not an object")
    return body


def _checked(answer: httpx.Response) -> httpx.Response:
    """The answer when it is a success; the store's error for a refusal."""
    status = answer.status_code
    if status < 400:
        return answer
    try:
        detail = str(answer.json().get("detail", ""))
    except (ValueError, AttributeError):
        detail = answer.text[:200]
    if status == 404:
        raise SessionNotFoundError(detail)
    if status == 409:
        raise TurnLostError(detail)
    if status == 422:
        raise WorkerRefusedError(detail)
    if status == 400:
        raise InvalidValueError(detail)
    raise WorkerApiRefusedError(f"the worker API answered {status}: {detail}")
