# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The turn broker of a worker in a process of its own, over HTTP: the routes of
``web/worker_api.py``, at the server that spawned it, with the secret it was handed.

A worker starts before its server listens, and stops while it still does, so a connection
refused is tried again, for up to ``CONNECT_PATIENCE`` seconds: the request was never sent.
Nothing else is tried again. The cancels are polled for by a task of their own, from
``listen_for_cancels`` until ``close``; one lost while the poll fails is named by the next
renewal of the leases.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any

import httpx

from robinauts.controller.contract.domain import (
    Cursor,
    Document,
    Session,
    SessionNotFoundError,
    StoredEvent,
    StoredMessage,
    Task,
    Turn,
    TurnLostError,
    TurnState,
)
from robinauts.controller.contract.ports import TurnBroker
from robinauts.web.worker_wire import (
    PREFIX,
    SESSION_NOT_FOUND,
    TURN_LOST,
    WORKER_KEEP_ALIVE_SECONDS,
    AppendRequest,
    CancelsResponse,
    ClaimRequest,
    FinishRequest,
    FoundTurnResponse,
    MessagesResponse,
    RenewRequest,
    RenewResponse,
    SessionResponse,
    SessionsResponse,
    TaskResponse,
    TasksResponse,
    WaitRequest,
    WaitResponse,
    WorkerRefusal,
)

_log = logging.getLogger(__name__)

REQUEST_SECONDS = 15.0
"""How long a request may take, past what it asks the server to wait: under the lease (90 s),
so that a hung renewal or final write fails while the turn still holds it."""

CONNECT_PATIENCE = 30.0
"""How long a request keeps trying a connection the server refuses."""

CANCEL_POLL_SECONDS = 25.0
"""How long one poll for cancels waits on the server."""

POLL_RETRY_SECONDS = 1.0
"""How long the poll for cancels waits after a failure before it polls again."""


class QuietRequests(logging.Filter):
    """Leaves out of httpx's log the worker's requests to the core, one per event a turn
    writes, which its server logs already; the engines' requests stay."""

    def __init__(self, url: str) -> None:
        super().__init__()
        self._root = url.rstrip("/") + PREFIX

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args if isinstance(record.args, tuple) else ()
        return not any(str(arg).startswith(self._root) for arg in args)


class BrokerRefusedError(Exception):
    """The server answered a worker's request with a status that is not one of the refusals
    the worker acts on."""


def _ids(path: str, *ids: object) -> str:
    return path.format(*(str(i) for i in ids))


class HttpTurnBroker(TurnBroker):
    def __init__(
        self,
        url: str,
        secret: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        connect_patience: float = CONNECT_PATIENCE,
    ) -> None:
        """``url`` is the server's root; ``transport``, one in a test's place of the network."""
        self._client = httpx.AsyncClient(
            base_url=url.rstrip("/") + PREFIX,
            headers={"Authorization": f"Bearer {secret}"},
            timeout=REQUEST_SECONDS,
            # httpx's own limits, but for how long an idle connection is reused.
            limits=httpx.Limits(
                max_connections=100,
                max_keepalive_connections=20,
                keepalive_expiry=WORKER_KEEP_ALIVE_SECONDS,
            ),
            transport=transport,
            # The server is this machine's, and no proxy stands between them.
            trust_env=False,
        )
        self._patience = connect_patience
        self._stops: list[Callable[[uuid.UUID], object]] = []
        self._polling: asyncio.Task[None] | None = None

    async def _call(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        params: Mapping[str, Any] | None = None,
        waits: float = 0.0,
    ) -> Any:
        """The answer's JSON, ``None`` for an empty one. ``waits`` is how long the server is
        asked to hold the request, which its time limit adds to."""
        loop = asyncio.get_running_loop()
        give_up = loop.time() + self._patience
        delay = 0.05
        while True:
            try:
                response = await self._client.request(
                    method,
                    path,
                    json=None if body is None else body.model_dump(mode="json"),
                    params=params,
                    timeout=REQUEST_SECONDS + waits,
                )
                break
            except httpx.ConnectError:
                if loop.time() + delay > give_up:
                    raise
                await asyncio.sleep(delay)
                delay = min(delay * 2, 1.0)
        if response.is_success:
            return response.json() if response.content else None
        refusal = None
        try:
            refusal = WorkerRefusal.model_validate_json(response.content)
        except ValueError:
            pass
        if refusal is not None and refusal.error == TURN_LOST:
            raise TurnLostError(refusal.detail)
        if refusal is not None and refusal.error == SESSION_NOT_FOUND:
            raise SessionNotFoundError(refusal.detail)
        raise BrokerRefusedError(f"the server answered {response.status_code} to {method} {path}")

    async def claim_tasks(self, now: datetime, until: datetime, limit: int) -> list[Task]:
        answer = await self._call(
            "POST", "/tasks/claim", body=ClaimRequest(now=now, until=until, limit=limit)
        )
        return TasksResponse.model_validate(answer).tasks

    async def wait_for_queued(self, now: datetime, timeout: float) -> bool:
        answer = await self._call(
            "POST", "/tasks/wait", body=WaitRequest(now=now, timeout=timeout), waits=timeout
        )
        return WaitResponse.model_validate(answer).queued

    async def renew_leases(
        self, tasks: Sequence[uuid.UUID], now: datetime, until: datetime
    ) -> list[uuid.UUID]:
        answer = await self._call(
            "POST", "/tasks/renew", body=RenewRequest(tasks=list(tasks), now=now, until=until)
        )
        return RenewResponse.model_validate(answer).stop

    async def listen_for_cancels(self, stop: Callable[[uuid.UUID], object]) -> None:
        self._stops.append(stop)
        if self._polling is None:
            self._polling = asyncio.create_task(self._poll_cancels(), name="cancels")

    async def _poll_cancels(self) -> None:
        cursor: int | None = None
        while True:
            params: dict[str, Any] = {"timeout": CANCEL_POLL_SECONDS}
            if cursor is not None:
                params["after"] = cursor
            try:
                answer = await self._call(
                    "GET", "/cancels", params=params, waits=CANCEL_POLL_SECONDS
                )
            except Exception:
                _log.warning("the worker could not hear of cancels", exc_info=True)
                await asyncio.sleep(POLL_RETRY_SECONDS)
                continue
            heard = CancelsResponse.model_validate(answer)
            cursor = heard.cursor
            for task in heard.tasks:
                for stop in self._stops:
                    stop(task)

    async def get_task(self, task: uuid.UUID) -> Task | None:
        answer = await self._call("GET", _ids("/tasks/{}", task))
        return TaskResponse.model_validate(answer).task

    async def find_turn(self, turn: uuid.UUID) -> tuple[uuid.UUID, Turn] | None:
        answer = await self._call("GET", _ids("/turns/{}", turn))
        found = FoundTurnResponse.model_validate(answer)
        if found.owner is None or found.turn is None:
            return None
        return found.owner, found.turn

    async def get_session(self, owner: uuid.UUID, session: uuid.UUID) -> Session:
        answer = await self._call("GET", _ids("/users/{}/sessions/{}", owner, session))
        return SessionResponse.model_validate(answer).session

    async def sessions_of(
        self, owner: uuid.UUID, limit: int, before: Cursor | None
    ) -> list[Session]:
        params: dict[str, Any] = {"limit": limit}
        if before is not None:
            params["before_updated_at"] = before[0].isoformat()
            params["before_id"] = str(before[1])
        answer = await self._call("GET", _ids("/users/{}/sessions", owner), params=params)
        return SessionsResponse.model_validate(answer).sessions

    async def messages_of(self, owner: uuid.UUID, session: uuid.UUID) -> list[Document]:
        answer = await self._call("GET", _ids("/users/{}/sessions/{}/messages", owner, session))
        return list(MessagesResponse.model_validate(answer).messages)

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
        body = AppendRequest(document=dict(document), written_at=written_at, expires_at=expires_at)
        path = _ids("/users/{}/sessions/{}/turns/{}/events/{}", owner, session, turn, position)
        await self._call("PUT", path, body=body)

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
        body = FinishRequest(
            state=state,
            ended_at=ended_at,
            error=error,
            answer=answer,
            events=list(events),
            updated_at=updated_at,
        )
        path = _ids("/users/{}/sessions/{}/turns/{}/finish", owner, session, turn)
        await self._call("POST", path, body=body)

    async def close(self) -> None:
        if self._polling is not None:
            polling, self._polling = self._polling, None
            polling.cancel()
            await asyncio.gather(polling, return_exceptions=True)
        await self._client.aclose()
