# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The API's routes a worker in a process of its own reaches the core through: the operations
of ``TurnBroker`` (``web/contract/broker/wire.py``), served by the core over the
controller's store.

There is one such worker per worker the configuration names (``AgentConfig.worker``), each
with a secret of its own, which the server handed it at its start and ``WorkerAccess`` holds.
They answer only while a worker runs, and only those secrets: 404 while none runs, 401 for any
other credential. The secret says which worker asks, and each is served through its own
broker: a worker claims and waits for the tasks of its own agents, and no other worker's,
whatever it sends. The cancels a worker hears are numbered here as they come, every worker's
alike, and it polls past the last it saw; one it misses, the next renewal of its leases names.
A wait gives up as soon as the workers are stopped, so that a server that stops them is not
held by their own long polls.
"""

from __future__ import annotations

import asyncio
import hmac
import uuid
from collections import deque
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from typing import Annotated, Any, TypeVar

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import AwareDatetime

from robinauts.controller.contract.broker.ports import TurnBroker
from robinauts.shared_domain.domain import DEFAULT_WORKER, SessionNotFoundError, TurnLostError
from robinauts.web.contract.broker.wire import (
    MAX_WAIT_SECONDS,
    PREFIX,
    SESSION_NOT_FOUND,
    TURN_LOST,
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
)

T = TypeVar("T")

KEPT_CANCELS = 1024
"""How many of the latest cancels a poll can still be answered with."""


class WorkerAccess:
    """What lets the worker processes in: the secret each was handed while it runs, by
    worker, and the cancels they poll for."""

    def __init__(self, keep: int = KEPT_CANCELS) -> None:
        self._secrets: dict[str, str] = {}
        self._closed = asyncio.Event()
        self._closed.set()
        self._last = 0
        self._kept: deque[tuple[int, uuid.UUID]] = deque(maxlen=keep)
        self._changed = asyncio.Event()

    @property
    def is_open(self) -> bool:
        return bool(self._secrets)

    def open(self, secret: str, worker: str = DEFAULT_WORKER) -> None:
        """Let in, as ``worker``, the process that presents ``secret``."""
        if not self._secrets:
            self._closed = asyncio.Event()
        self._secrets[worker] = secret

    def close(self) -> None:
        """Let no worker in any more, and end the waits of those that were."""
        self._secrets = {}
        self._closed.set()

    def admitted(self, authorization: str | None) -> str | None:
        """The worker whose secret ``authorization`` presents, or ``None``. Every secret is
        compared, so that how long a refusal takes says nothing of which came close."""
        scheme, _, given = (authorization or "").partition(" ")
        found = None
        for worker, secret in self._secrets.items():
            if hmac.compare_digest(given.encode(), secret.encode()):
                found = worker
        return found if scheme.lower() == "bearer" else None

    def admits(self, authorization: str | None) -> bool:
        return self.admitted(authorization) is not None

    def stop_requested(self, task: uuid.UUID) -> None:
        """A task asked to stop, from any process: what the core's listener is given."""
        self._last += 1
        self._kept.append((self._last, task))
        woken, self._changed = self._changed, asyncio.Event()
        woken.set()

    async def cancels_after(self, after: int | None, timeout: float) -> tuple[int, list[uuid.UUID]]:
        """The cursor now, and the cancels past ``after``, waiting up to ``timeout`` for one
        when there is none yet. With no cursor, or one this server never gave, the cursor
        alone: a worker hears the cancels from its first poll on."""
        if after is None or after > self._last:
            return self._last, []
        if after == self._last:
            await self.unless_closed(self._changed.wait(), None, timeout)
        return self._last, [task for n, task in self._kept if n > after]

    async def unless_closed(
        self, waiting: Coroutine[Any, Any, T], otherwise: T, timeout: float | None = None
    ) -> T:
        """What ``waiting`` comes to, or ``otherwise`` once access closes or ``timeout`` has
        passed, whichever is first."""
        waited = asyncio.ensure_future(waiting)
        closing = asyncio.ensure_future(self._closed.wait())
        try:
            await asyncio.wait({waited, closing}, timeout=timeout, return_when="FIRST_COMPLETED")
        finally:
            waited.cancel()
            closing.cancel()
            await asyncio.gather(waited, closing, return_exceptions=True)
        if waited.cancelled():
            return otherwise
        return waited.result()


class _Refusing(APIRoute):
    """A route whose ``TurnLostError`` and ``SessionNotFoundError`` answer the worker's
    refusals, before the app's own handlers answer them for the frontend."""

    def get_route_handler(self) -> Callable[[Request], Awaitable[Response]]:
        handler = super().get_route_handler()

        async def refusing(request: Request) -> Response:
            try:
                return await handler(request)
            except TurnLostError as lost:
                return JSONResponse({"error": TURN_LOST, "detail": str(lost)}, status_code=409)
            except SessionNotFoundError as missing:
                body = {"error": SESSION_NOT_FOUND, "detail": str(missing)}
                return JSONResponse(body, status_code=404)

        return refusing


def worker_router(brokers: Mapping[str, TurnBroker], access: WorkerAccess) -> APIRouter:
    """The routes of ``PREFIX`` for the workers ``access`` lets in, each over its broker in
    ``brokers``, by the worker's name."""

    def worker_only(request: Request) -> TurnBroker:
        """The broker of the worker that asks; FastAPI runs it once a request, for the router
        and for the route that names it."""
        if not access.is_open:
            raise HTTPException(404)
        worker = access.admitted(request.headers.get("authorization"))
        if worker is None:
            raise HTTPException(401, headers={"WWW-Authenticate": "Bearer"})
        broker = brokers.get(worker)
        if broker is None:
            raise HTTPException(404)
        return broker

    asking = Depends(worker_only)

    router = APIRouter(
        prefix=PREFIX,
        route_class=_Refusing,
        dependencies=[Depends(worker_only)],
        include_in_schema=False,
    )

    @router.post("/tasks/claim")
    async def claim_tasks(body: ClaimRequest, broker: TurnBroker = asking) -> TasksResponse:
        tasks = await broker.claim_tasks(body.now, body.until, body.limit)
        return TasksResponse(tasks=tasks)

    @router.post("/tasks/wait")
    async def wait_for_queued(body: WaitRequest, broker: TurnBroker = asking) -> WaitResponse:
        queued = await access.unless_closed(broker.wait_for_queued(body.now, body.timeout), False)
        return WaitResponse(queued=queued)

    @router.post("/tasks/renew")
    async def renew_leases(body: RenewRequest, broker: TurnBroker = asking) -> RenewResponse:
        return RenewResponse(stop=await broker.renew_leases(body.tasks, body.now, body.until))

    @router.get("/tasks/{task_id}")
    async def get_task(task_id: uuid.UUID, broker: TurnBroker = asking) -> TaskResponse:
        task = await broker.get_task(task_id)
        return TaskResponse(task=task)

    @router.get("/cancels")
    async def cancels(
        after: Annotated[int | None, Query(ge=0)] = None,
        timeout: Annotated[float, Query(ge=0, le=MAX_WAIT_SECONDS)] = 0.0,
    ) -> CancelsResponse:
        cursor, tasks = await access.cancels_after(after, timeout)
        return CancelsResponse(cursor=cursor, tasks=tasks)

    @router.get("/turns/{turn_id}")
    async def find_turn(turn_id: uuid.UUID, broker: TurnBroker = asking) -> FoundTurnResponse:
        found = await broker.find_turn(turn_id)
        if found is None:
            return FoundTurnResponse(owner=None, turn=None)
        owner, turn = found
        return FoundTurnResponse(owner=owner, turn=turn)

    @router.get("/users/{owner}/sessions")
    async def sessions_of(
        owner: uuid.UUID,
        limit: Annotated[int, Query(ge=1, le=1000)],
        before_updated_at: AwareDatetime | None = None,
        before_id: uuid.UUID | None = None,
        broker: TurnBroker = asking,
    ) -> SessionsResponse:
        before = None
        if before_updated_at is not None and before_id is not None:
            before = (before_updated_at, before_id)
        sessions = await broker.sessions_of(owner, limit, before)
        return SessionsResponse(sessions=sessions)

    @router.get("/users/{owner}/sessions/{session_id}")
    async def get_session(
        owner: uuid.UUID, session_id: uuid.UUID, broker: TurnBroker = asking
    ) -> SessionResponse:
        return SessionResponse(session=await broker.get_session(owner, session_id))

    @router.get("/users/{owner}/sessions/{session_id}/messages")
    async def messages_of(
        owner: uuid.UUID, session_id: uuid.UUID, broker: TurnBroker = asking
    ) -> MessagesResponse:
        documents = await broker.messages_of(owner, session_id)
        return MessagesResponse(messages=[dict(d) for d in documents])

    @router.put(
        "/users/{owner}/sessions/{session_id}/turns/{turn_id}/events/{position}",
        status_code=204,
    )
    async def append_event(
        owner: uuid.UUID,
        session_id: uuid.UUID,
        turn_id: uuid.UUID,
        position: Annotated[int, Path(ge=1)],
        body: AppendRequest,
        broker: TurnBroker = asking,
    ) -> None:
        await broker.append_event(
            owner, session_id, turn_id, position, body.document, body.written_at, body.expires_at
        )

    @router.post("/users/{owner}/sessions/{session_id}/turns/{turn_id}/finish", status_code=204)
    async def finish_turn(
        owner: uuid.UUID,
        session_id: uuid.UUID,
        turn_id: uuid.UUID,
        body: FinishRequest,
        broker: TurnBroker = asking,
    ) -> None:
        await broker.finish_turn(
            owner,
            session_id,
            turn_id,
            body.state,
            body.ended_at,
            body.error,
            body.answer,
            body.events,
            body.updated_at,
        )

    return router
