# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Robinauts' permissions and roles, as data that oneberth registers (``docs/architecture/web.md``,
"Roles and permissions").

Every route of the component declares one permission. oneberth checks the permission, and the
worker name a worker's grant is bound to. The checks on ownership, such as whose conversation
this is, stay in the controller.

- ``robinauts/user`` is every person's, the role an allow entry gives.
- ``robinauts/admin`` is for the people the configuration names. It holds what a person holds:
  no route is an admin's alone yet.
- ``robinauts/worker`` is a service token's, bound to one worker name. It holds the routes of
  ``/internal/worker`` and nothing else, so they are served on the worker listener. Those
  routes name no worker: each declares its permit ``bound``, and acts on the token's worker.
- ``robinauts/handler`` is a service token's: the handlers of the flows Robinauts ships hold
  it. They act for the person who started their run, whom a header names.
"""

from __future__ import annotations

from oneberth.identity import Role, RoleTable

APP = "robinauts"

AGENTS_READ = "robinauts/agents:read"
"""The agents and the models."""
APPS_USE = "robinauts/apps:use"
"""The MCP apps of the side panel: their list, their views and their tools."""
PROJECTS_READ = "robinauts/projects:read"
PROJECTS_WRITE = "robinauts/projects:write"
CONVERSATIONS_READ = "robinauts/conversations:read"
"""The list, one conversation, the change feed, and the stream of a turn."""
CONVERSATIONS_WRITE = "robinauts/conversations:write"
"""Rename, archive, delete, fork, and choose the model."""
TURNS_START = "robinauts/turns:start"
"""A new conversation, a message, an edit, a regeneration, a retry, a resume, a cancel."""
FLOWS_READ = "robinauts/flows:read"
FLOWS_START = "robinauts/flows:start"

TASKS_WORK = "robinauts/tasks:work"
"""A worker's tasks: claim, wait, renew, read, the cancels, and a turn's owner."""
SESSIONS_READ = "robinauts/sessions:read"
"""The sessions and the messages a worker's turn reads."""
TURNS_WRITE = "robinauts/turns:write"
"""The events and the end of a turn a worker runs."""

PERSON_PERMISSIONS = frozenset(
    {
        AGENTS_READ,
        APPS_USE,
        PROJECTS_READ,
        PROJECTS_WRITE,
        CONVERSATIONS_READ,
        CONVERSATIONS_WRITE,
        TURNS_START,
        FLOWS_READ,
        FLOWS_START,
    }
)
WORKER_PERMISSIONS = frozenset({TASKS_WORK, SESSIONS_READ, TURNS_WRITE})
HANDLER_PERMISSIONS = frozenset({AGENTS_READ, CONVERSATIONS_READ, TURNS_START})
"""What a flow's handler does for the person who started its run: open a conversation, say
something in it, and read the answer."""

USER_ROLE = "robinauts/user"
ADMIN_ROLE = "robinauts/admin"
WORKER_ROLE = "robinauts/worker"
HANDLER_ROLE = "robinauts/handler"

WORKER_RESOURCE = "worker"
"""What a worker's grant is bound to: the worker's name, as the configuration names it."""

ROBINAUTS_ROLES = RoleTable(
    app=APP,
    permissions=PERSON_PERMISSIONS | WORKER_PERMISSIONS,
    roles=(
        Role(USER_ROLE, PERSON_PERMISSIONS, people=True),
        Role(ADMIN_ROLE, PERSON_PERMISSIONS, people=True),
        Role(WORKER_ROLE, WORKER_PERMISSIONS, resource=WORKER_RESOURCE),
        Role(HANDLER_ROLE, HANDLER_PERMISSIONS),
    ),
    default=USER_ROLE,
)
"""Robinauts' table, which its component gives oneberth."""
