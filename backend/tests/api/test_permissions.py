# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The permission each route of Robinauts' component declares, and oneberth's check of it on a
composed backend (``web/roles.py``): a principal without the permission is refused, a worker's
token reaches its own worker's routes alone, and the worker listener serves those routes and
nothing else of the component.

A new route fails ``test_every_route_is_in_the_table`` until it is added to ``PERMISSION_OF``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import fastapi.routing
import httpx
import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from neorc_core import QueueKind
from oneberth.web import testing

from robinauts.cli.composition import compose
from robinauts.controller.contract.domain import Identity as RobinautsIdentity
from robinauts.orchestrator.contract.domain import (
    HANDLER_QUEUE,
    HANDLER_QUEUE_PERMISSION,
    HANDLER_TOKEN_PATH,
)
from robinauts.shared_domain.domain import (
    AgentConfig,
    Config,
    ModelConfig,
    ProviderConfig,
    ProviderKind,
    StorageConfig,
    StorageKind,
)
from robinauts.web import roles
from robinauts.web.app import NAME, new_robinauts_webapp
from robinauts.web.worker_api import WorkerAccess
from util.aio import asyncio_test
from util.app import backend_of

CONFIG = Config(
    providers={"echo": ProviderConfig("echo", ProviderKind.ANTHROPIC, "ECHO_API_KEY")},
    models={"echo": ModelConfig("echo", provider="echo", name="echo", title="Echo")},
    agents={
        "echo": AgentConfig("echo", title="Echo", system_prompt="", models=("echo",), engine="echo")
    },
)

W = "/internal/worker"
OWNED = "/users/{owner}/sessions/{session_id}"
PERMISSION_OF: dict[tuple[str, str], str] = {
    ("GET", "/api/agents"): roles.AGENTS_READ,
    ("GET", "/api/models"): roles.AGENTS_READ,
    ("GET", "/api/flows"): roles.FLOWS_READ,
    ("POST", "/api/flows/{flow}/runs"): roles.FLOWS_START,
    ("GET", "/api/apps"): roles.APPS_USE,
    ("GET", "/api/apps/{server}/{tool}/view"): roles.APPS_USE,
    ("POST", "/api/apps/{server}/tools/{tool}"): roles.APPS_USE,
    ("GET", "/api/projects"): roles.PROJECTS_READ,
    ("POST", "/api/projects"): roles.PROJECTS_WRITE,
    ("GET", "/api/conversations"): roles.CONVERSATIONS_READ,
    ("GET", "/api/changes"): roles.CONVERSATIONS_READ,
    ("GET", "/api/conversations/{conversation_id}"): roles.CONVERSATIONS_READ,
    ("PATCH", "/api/conversations/{conversation_id}"): roles.CONVERSATIONS_WRITE,
    ("POST", "/api/conversations/{conversation_id}/archive"): roles.CONVERSATIONS_WRITE,
    ("DELETE", "/api/conversations/{conversation_id}"): roles.CONVERSATIONS_WRITE,
    ("PUT", "/api/conversations/{conversation_id}/model"): roles.CONVERSATIONS_WRITE,
    ("POST", "/api/conversations/{conversation_id}/fork"): roles.CONVERSATIONS_WRITE,
    ("POST", "/api/turns"): roles.TURNS_START,
    ("POST", "/api/conversations/{conversation_id}/turns"): roles.TURNS_START,
    ("GET", "/api/conversations/{conversation_id}/runs/{run_id}/events"): (
        roles.CONVERSATIONS_READ
    ),
    ("POST", "/api/conversations/{conversation_id}/runs/{run_id}/cancel"): roles.TURNS_START,
    ("POST", f"{W}/tasks/claim"): roles.TASKS_WORK,
    ("POST", f"{W}/tasks/wait"): roles.TASKS_WORK,
    ("POST", f"{W}/tasks/renew"): roles.TASKS_WORK,
    ("GET", f"{W}/tasks/{{task_id}}"): roles.TASKS_WORK,
    ("GET", f"{W}/cancels"): roles.TASKS_WORK,
    ("GET", f"{W}/turns/{{turn_id}}"): roles.TASKS_WORK,
    ("GET", f"{W}/users/{{owner}}/sessions"): roles.SESSIONS_READ,
    ("GET", f"{W}{OWNED}"): roles.SESSIONS_READ,
    ("GET", f"{W}{OWNED}/messages"): roles.SESSIONS_READ,
    ("PUT", f"{W}{OWNED}/turns/{{turn_id}}/events/{{position}}"): roles.TURNS_WRITE,
    ("POST", f"{W}{OWNED}/turns/{{turn_id}}/finish"): roles.TURNS_WRITE,
    ("POST", HANDLER_TOKEN_PATH): HANDLER_QUEUE_PERMISSION,
}
"""Every route of the component, by method and path below ``/robinauts``, and the permission
it declares."""

BOUND = frozenset(route for route in PERMISSION_OF if route[1].startswith(W))
"""The routes that act on the worker a token is bound to, and name none in their path: each
declares its permit ``bound``. Every other route names its resource, or serves every one."""


def declared() -> dict[tuple[str, str], list[tuple[str, bool]]]:
    """The permissions each route of the component declares, with those of the routers above
    it, as oneberth reads them, each with whether it is bound."""
    composed = compose(CONFIG, storage=StorageConfig(StorageKind.IN_PROCESS), secret_for={}.get)
    component = new_robinauts_webapp(
        composed.controller,
        composed.brokers,
        worker_access=WorkerAccess(),
        config=CONFIG,
        secret_for={}.get,
    )
    app = FastAPI()
    app.include_router(component.router)
    found: dict[tuple[str, str], list[tuple[str, bool]]] = {}
    for route in fastapi.routing.iter_route_contexts(app.routes):
        if not isinstance(getattr(route, "original_route", route), APIRoute):
            continue
        permits: list[tuple[str, bool]] = []
        stack = [route.dependant]
        while stack:
            current = stack.pop()
            permit = getattr(current.call, "__oneberth_permit__", None)
            if permit is not None:
                permits.append((permit.permission, permit.bound))
            stack.extend(current.dependencies)
        for method in route.methods:
            found[(method, route.path)] = permits
    return found


DECLARED = declared()


def test_every_route_is_in_the_table() -> None:
    assert sorted(DECLARED) == sorted(PERMISSION_OF)


@pytest.mark.parametrize(("method", "path"), sorted(PERMISSION_OF))
def test_a_route_declares_its_one_permission(method: str, path: str) -> None:
    route = (method, path)
    assert DECLARED.get(route) == [(PERMISSION_OF[route], route in BOUND)]


@pytest.mark.parametrize("permission", sorted(roles.PERSON_PERMISSIONS))
def test_a_person_s_two_roles_hold_every_permission_of_the_interface(permission: str) -> None:
    held = {role.name: role.permissions for role in roles.ROBINAUTS_ROLES.roles}
    assert permission in held[roles.USER_ROLE]
    assert permission in held[roles.ADMIN_ROLE]
    assert permission not in held[roles.WORKER_ROLE]


def concrete(path: str) -> str:
    """``path`` with every parameter filled in."""
    for name in ("conversation_id", "run_id", "task_id", "turn_id", "owner", "session_id"):
        path = path.replace(f"{{{name}}}", str(uuid.uuid4()))
    return (
        path.replace("{flow}", "f")
        .replace("{server}", "s")
        .replace("{tool}", "t")
        .replace("{position}", "1")
        .replace("{queue}", HANDLER_QUEUE)
    )


def call(client: httpx.AsyncClient, method: str, path: str, **headers: str) -> Any:
    body = None if method in ("GET", "DELETE") else {}
    return client.request(method, f"/{NAME}{concrete(path)}", json=body, headers=headers)


@asynccontextmanager
async def composed_backend() -> AsyncIterator[Any]:
    """A backend signing people in through a provider, on two workers: ``default`` and
    ``special``, each let in as a process of its own would be, and neither running: what is
    queued stays queued for the test to claim."""
    from dataclasses import replace

    special = replace(CONFIG.agents["echo"], id="special", worker="special")
    config = replace(CONFIG, agents={**CONFIG.agents, "special": special})
    composed = compose(config, storage=StorageConfig(StorageKind.IN_PROCESS), secret_for={}.get)
    backend = backend_of(composed, secret_for={}.get, oneberth=testing.config_for(NAME))
    async with testing.running(backend.composition):
        await backend.lifecycle.stop_worker()
        for worker in composed.workers:
            backend.lifecycle.access.open(worker)
        yield backend


@pytest.mark.parametrize(("method", "path"), sorted(PERMISSION_OF))
@asyncio_test
async def test_a_principal_without_the_permission_is_refused(method: str, path: str) -> None:
    async with composed_backend() as backend:
        composition = backend.composition
        neorc_ci = await testing.service_token(composition, "neorc/ci")
        worker = await testing.service_token(composition, roles.WORKER_ROLE, "default")
        async with testing.client_of(composition) as client:
            other_role = await call(client, method, path, **testing.bearer(neorc_ci))
            assert other_role.status_code == 403, other_role.text
            if path.startswith(W):
                async with testing.client_of(composition) as person:
                    await testing.signed_in(composition, person)
                    as_person = await call(person, method, path)
                assert as_person.status_code == 403, as_person.text
            else:
                as_worker = await call(client, method, path, **testing.bearer(worker))
                assert as_worker.status_code == 403, as_worker.text


@pytest.mark.parametrize(("method", "path"), sorted(BOUND))
@asyncio_test
async def test_a_worker_s_token_passes_the_permit_of_each_worker_route(
    method: str, path: str
) -> None:
    async with composed_backend() as backend:
        composition = backend.composition
        worker = await testing.service_token(composition, roles.WORKER_ROLE, "default")
        async with testing.client_of(composition, worker=True) as listener:
            answer = await call(listener, method, path, **testing.bearer(worker))
        assert answer.status_code != 403, answer.text


@asyncio_test
async def test_a_worker_reaches_its_own_worker_s_routes_alone() -> None:
    async with composed_backend() as backend:
        composition = backend.composition
        default = await testing.service_token(composition, roles.WORKER_ROLE, "default")
        special = await testing.service_token(composition, roles.WORKER_ROLE, "special")
        gone = await testing.service_token(composition, roles.WORKER_ROLE, "gone")
        # A turn queued for the worker ``special``, whose stream nobody waits on.
        controller = backend.composed.controller
        ada = await controller.ensure_user(RobinautsIdentity("test", "ada"), id=uuid.uuid4())
        await controller.start_session(ada, agent="special", model="echo", text="hello")
        now = datetime.now(UTC).isoformat()
        claim = {"now": now, "until": now, "limit": 10}
        async with testing.client_of(composition, worker=True) as listener:
            for token, tasks in ((default, 0), (special, 1)):
                claimed = await listener.post(
                    f"/{NAME}{W}/tasks/claim", json=claim, headers=testing.bearer(token)
                )
                assert claimed.status_code == 200, claimed.text
                assert len(claimed.json()["tasks"]) == tasks
            # A token bound to a worker name the server runs no worker of reaches nothing.
            refused = await listener.post(
                f"/{NAME}{W}/tasks/claim", json=claim, headers=testing.bearer(gone)
            )
            assert refused.status_code == 404


@asyncio_test
async def test_the_worker_listener_serves_the_worker_routes_and_nothing_else() -> None:
    async with composed_backend() as backend:
        composition = backend.composition
        worker = await testing.service_token(composition, roles.WORKER_ROLE, "default")
        async with testing.client_of(composition, worker=True) as listener:
            cancels = await listener.get(f"/{NAME}{W}/cancels", headers=testing.bearer(worker))
            for path in ("/api/agents", "/api/conversations", "/openapi.json"):
                answer = await listener.get(f"/{NAME}{path}", headers=testing.bearer(worker))
                assert answer.status_code == 404, (path, answer.text)
            assert (await listener.get("/auth/session")).status_code == 404
            assert (await listener.get("/ui/")).status_code == 404
        assert cancels.status_code == 200, cancels.text
        async with testing.client_of(composition) as front:
            same = await front.get(f"/{NAME}{W}/cancels", headers=testing.bearer(worker))
            assert same.status_code == 200, same.text


@asyncio_test
async def test_the_worker_of_the_handlers_queue_gets_the_handlers_token() -> None:
    async with composed_backend() as backend:
        composition = backend.composition
        manager = backend.orchestrator.manager
        await manager.declare_queue(HANDLER_QUEUE, QueueKind.WORKER)
        await manager.declare_queue("other", QueueKind.WORKER)
        handlers = await testing.service_token(composition, "neorc/worker", HANDLER_QUEUE)
        other = await testing.service_token(composition, "neorc/worker", "other")
        async with testing.client_of(composition, worker=True) as listener:
            given = await listener.post(
                f"/{NAME}{concrete(HANDLER_TOKEN_PATH)}", json={}, headers=testing.bearer(handlers)
            )
            assert given.status_code == 201, given.text
            # A token of another queue's worker: its own queue is not the handlers'; the
            # handlers' queue is not its own.
            for queue, status in ((HANDLER_QUEUE, 403), ("other", 404)):
                path = HANDLER_TOKEN_PATH.format(queue=queue)
                refused = await listener.post(
                    f"/{NAME}{path}", json={}, headers=testing.bearer(other)
                )
                assert refused.status_code == status, refused.text
        token = testing.bearer(given.json()["token"])
        async with testing.client_of(composition) as front:
            assert (await front.get(f"/{NAME}/api/agents", headers=token)).status_code == 200
            listed = await front.get(f"/{NAME}/api/conversations", headers=token)
            assert listed.status_code == 403, listed.text
