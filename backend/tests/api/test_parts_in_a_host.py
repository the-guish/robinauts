# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Robinauts as a part (``docs/architecture/web.md``, "Robinauts as a part"). A host of another
project makes Robinauts' parts with one call and composes them beside a component of its own.
The real process serves the three under their names, signs the local user in, uploads the flows
through the composition it was told of, and keeps Robinauts' records in the data directory."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import httpx
import pytest

from util import stack

pytestmark = pytest.mark.io

HOST = '''
"""A stand-in for another project's host: Robinauts' parts beside a component of its own."""

import os
import sys

from fastapi import APIRouter
from oneberth.identity import Role, RoleTable
from oneberth.web import Component, Listen, compose_app, permit, run

from robinauts.cli.backend import robinauts_parts

READ = "notes/notes:read"
ROLES = RoleTable(
    app="notes",
    permissions=frozenset({READ}),
    roles=(Role("notes/user", frozenset({READ}), people=True),),
    default="notes/user",
)
router = APIRouter()


@router.get("/api/notes", dependencies=[permit(READ)])
async def notes() -> list[str]:
    return ["a note of the host's"]


def port(name: str) -> int:
    return int(sys.argv[sys.argv.index(name) + 1])


front, worker = Listen("127.0.0.1", port("--port")), Listen("127.0.0.1", port("--worker-port"))
parts = robinauts_parts(os.environ, front=front, worker=worker, local=True)
composition = compose_app(
    [*parts.components, Component(name="notes", roles=ROLES, router=router)],
    config=parts.oneberth,
    store=parts.store,
    local=parts.local,
)
parts.composed_into(composition)
run(composition, front, worker)
'''

FLOW = """
name: probe
version: 0.1.0
steps:
  pick:
    queue: default
    handler: robinauts_tasks:pick_number
"""


def test_a_host_composes_robinauts_parts_beside_a_component_of_its_own(tmp_path: Path) -> None:
    host = tmp_path / "host.py"
    host.write_text(HOST)
    config = tmp_path / "robinauts.toml"
    config.write_text("")
    flows = tmp_path / "flows"
    flows.mkdir()
    (flows / "probe.yaml").write_text(FLOW)
    environment = {
        **os.environ,
        "ROBINAUTS_CONFIG": str(config),
        "ROBINAUTS_DATA_DIR": str(tmp_path / "data"),
        "ROBINAUTS_FLOWS_DIR": str(flows),
    }
    for variable in ("ROBINAUTS_DATABASE_URL", "ONEBERTH_CONFIG"):
        environment.pop(variable, None)
    running, url = stack.start_local(environment, [sys.executable, str(host)])
    try:
        stack.wait_until_up(url, running)
        with httpx.Client(base_url=url, timeout=30.0) as client:
            assert client.get("/notes/api/notes").status_code == 401
            stack.signed_in(client, url)
            roles = client.get("/auth/session").json()["roles"]
            assert {"robinauts/user", "neorc/user", "notes/user"} <= set(roles)
            assert client.get("/notes/api/notes").json() == ["a note of the host's"]
            assert client.get("/robinauts/api/conversations").status_code == 200
            # The flows went up through the composition the parts were told of.
            listed = client.get("/robinauts/api/flows")
            assert [flow["name"] for flow in listed.json()["items"]] == ["probe"], listed.text
            assert client.get("/neorc/flows/probe").status_code == 200
    finally:
        running.terminate()
        assert running.wait(timeout=30) == 0
    assert sorted(p.name for p in (tmp_path / "data").glob("*.db")) == [
        "neorc.db",
        "oneberth.db",
        "robinauts.db",
    ]
