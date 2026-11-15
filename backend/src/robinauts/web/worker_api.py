# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The API's routes a worker in a process of its own reaches the core through: the operations
of ``TurnBroker`` (``web/contract/broker/wire.py``), served by the core over the
controller's store.

There is one such worker per worker the configuration names (``AgentConfig.worker``). Each
holds a oneberth service token of ``robinauts/worker``, bound to its name: the server mints it
at the worker's start and hands it over on the worker's standard input
(``cli/lifecycle.py``). oneberth checks the token and its permission before a route runs, and
serves these routes on the worker listener, where they are the only ones. No route names a
worker in its path: each declares its permit ``bound``, and acts on the worker the token is
bound to, which ``Access.bound_resource`` reads from the grant. That name says which worker
asks, and each is served through its own broker: a worker claims
and waits for the tasks of its own agents, and no other worker's, whatever it sends. They
answer only while the workers run: 404 while none does. The cancels a worker hears are
numbered here as they come, every worker's alike, and it polls past the last it saw; one it
misses, the next renewal of its leases names. A wait gives up as soon as the workers are
stopped, so that a server that stops them is not held by their own long polls.
"""

from __future__ import annotations

import asyncio
import uuid
from collections import deque
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from typing import Annotated, Any, TypeVar

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from oneberth.identity import Principal
from oneberth.web import Asking, permit
from pydantic import AwareDatetime

from robinauts.controller.contract.broker.ports import TurnBroker
from robinauts.shared_domain.domain import (
    DEFAULT_WORKER,
    SessionNotFoundError,
    TurnLostError,
)
from robinauts.web import roles
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
    """Whether the workers run, by name, and the cancels they poll for."""

    def __init__(
        self, keep: int = KEPT_CANCELS, heard: Callable[[str], None] | None = None
    ) -> None:
        """``heard`` is told the worker each admitted request comes from, by name."""
        self._heard = heard
        self._workers: set[str] = set()
        self._closed = asyncio.Event()
        self._closed.set()
        self._last = 0
        self._kept: deque[tuple[int, uuid.UUID]] = deque(maxlen=keep)
        self._changed = asyncio.Event()

    @property
    def is_open(self) -> bool:
        return bool(self._workers)

    def open(self, worker: str = DEFAULT_WORKER) -> None:
        """Let the worker ``worker`` in, by the token bound to its name."""
        if not self._workers:
            self._closed = asyncio.Event()
        self._workers.add(worker)

    def close(self) -> None:
        """Let no worker in any more, and end the waits of those that were."""
        self._workers = set()
        self._closed.set()

    def admitted(self, worker: str) -> bool:
        """Whether the worker ``worker`` runs and is let in; ``heard`` is told when it is."""
        if worker not in self._workers:
            return False
        if self._heard is not None:
            self._heard(worker)
        return True

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
        self,
        waiting: Coroutine[Any, Any, T],
        otherwise: T,
        timeout: float | None = None,
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
    ``brokers``, by the worker's name: the one its token is bound to."""

    def broker_of(permission: str) -> Callable[[Request, Principal], TurnBroker]:
        def worker_only(request: Request, principal: Asking) -> TurnBroker:
            """The broker of the worker that asks; FastAPI runs it once a request."""
            worker = request.app.state.oneberth.access.bound_resource(principal, permission)
            if worker is None or not access.admitted(worker):
                raise HTTPException(404)
            broker = brokers.get(worker)
            if broker is None:
                raise HTTPException(404)
            return broker

        return worker_only

    working = Depends(broker_of(roles.TASKS_WORK))
    reading = Depends(broker_of(roles.SESSIONS_READ))
    writing = Depends(broker_of(roles.TURNS_WRITE))

    router = APIRouter(prefix=PREFIX, route_class=_Refusing, include_in_schema=False)

    @router.post("/tasks/claim", dependencies=[permit(roles.TASKS_WORK, bound=True)])
    async def claim_tasks(body: ClaimRequest, broker: TurnBroker = working) -> TasksResponse:
        tasks = await broker.claim_tasks(body.now, body.until, body.limit)
        return TasksResponse(tasks=tasks)

    @router.post("/tasks/wait", dependencies=[permit(roles.TASKS_WORK, bound=True)])
    async def wait_for_queued(body: WaitRequest, broker: TurnBroker = working) -> WaitResponse:
        queued = await access.unless_closed(broker.wait_for_queued(body.now, body.timeout), False)
        return WaitResponse(queued=queued)

    @router.post("/tasks/renew", dependencies=[permit(roles.TASKS_WORK, bound=True)])
    async def renew_leases(body: RenewRequest, broker: TurnBroker = working) -> RenewResponse:
        return RenewResponse(stop=await broker.renew_leases(body.tasks, body.now, body.until))

    @router.get("/tasks/{task_id}", dependencies=[permit(roles.TASKS_WORK, bound=True)])
    async def get_task(task_id: uuid.UUID, broker: TurnBroker = working) -> TaskResponse:
        task = await broker.get_task(task_id)
        return TaskResponse(task=task)

    @router.get("/cancels", dependencies=[permit(roles.TASKS_WORK, bound=True)])
    async def cancels(
        after: Annotated[int | None, Query(ge=0)] = None,
        timeout: Annotated[float, Query(ge=0, le=MAX_WAIT_SECONDS)] = 0.0,
        _: TurnBroker = working,
    ) -> CancelsResponse:
        cursor, tasks = await access.cancels_after(after, timeout)
        return CancelsResponse(cursor=cursor, tasks=tasks)

    @router.get("/turns/{turn_id}", dependencies=[permit(roles.TASKS_WORK, bound=True)])
    async def find_turn(turn_id: uuid.UUID, broker: TurnBroker = working) -> FoundTurnResponse:
        found = await broker.find_turn(turn_id)
        if found is None:
            return FoundTurnResponse(owner=None, turn=None)
        owner, turn = found
        return FoundTurnResponse(owner=owner, turn=turn)

    @router.get("/users/{owner}/sessions", dependencies=[permit(roles.SESSIONS_READ, bound=True)])
    async def sessions_of(
        owner: uuid.UUID,
        limit: Annotated[int, Query(ge=1, le=1000)],
        before_updated_at: AwareDatetime | None = None,
        before_id: uuid.UUID | None = None,
        broker: TurnBroker = reading,
    ) -> SessionsResponse:
        before = None
        if before_updated_at is not None and before_id is not None:
            before = (before_updated_at, before_id)
        sessions = await broker.sessions_of(owner, limit, before)
        return SessionsResponse(sessions=sessions)

    @router.get(
        "/users/{owner}/sessions/{session_id}",
        dependencies=[permit(roles.SESSIONS_READ, bound=True)],
    )
    async def get_session(
        owner: uuid.UUID, session_id: uuid.UUID, broker: TurnBroker = reading
    ) -> SessionResponse:
        return SessionResponse(session=await broker.get_session(owner, session_id))

    @router.get(
        "/users/{owner}/sessions/{session_id}/messages",
        dependencies=[permit(roles.SESSIONS_READ, bound=True)],
    )
    async def messages_of(
        owner: uuid.UUID, session_id: uuid.UUID, broker: TurnBroker = reading
    ) -> MessagesResponse:
        documents = await broker.messages_of(owner, session_id)
        return MessagesResponse(messages=[dict(d) for d in documents])

    @router.put(
        "/users/{owner}/sessions/{session_id}/turns/{turn_id}/events/{position}",
        status_code=204,
        dependencies=[permit(roles.TURNS_WRITE, bound=True)],
    )
    async def append_event(
        owner: uuid.UUID,
        session_id: uuid.UUID,
        turn_id: uuid.UUID,
        position: Annotated[int, Path(ge=1)],
        body: AppendRequest,
        broker: TurnBroker = writing,
    ) -> None:
        await broker.append_event(
            owner,
            session_id,
            turn_id,
            position,
            body.document,
            body.written_at,
            body.expires_at,
        )

    @router.post(
        "/users/{owner}/sessions/{session_id}/turns/{turn_id}/finish",
        status_code=204,
        dependencies=[permit(roles.TURNS_WRITE, bound=True)],
    )
    async def finish_turn(
        owner: uuid.UUID,
        session_id: uuid.UUID,
        turn_id: uuid.UUID,
        body: FinishRequest,
        broker: TurnBroker = writing,
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
