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

