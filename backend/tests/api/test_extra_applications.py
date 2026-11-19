# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""An extra application: a component ``ROBINAUTS_COMPONENTS`` names, composed after Robinauts'
own and neorc's (``docs/architecture/web.md``, "Extra applications"). The real server serves
its routes under its name, behind oneberth's checks, and gives it the data directory, where
it keeps a file of its own beside the three. A name that gives no component, and a component
oneberth cannot compose, stop the start."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import httpx
import pytest

from util import stack

pytestmark = pytest.mark.io

NOTES = '''
"""A stand-in for an application of another project: its component, on a file of its own."""

import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import APIRouter
from oneberth.identity import Role, RoleTable
from oneberth.web import Component, permit

READ = "notes/notes:read"
ROLES = RoleTable(
    app="notes",
    permissions=frozenset({READ}),
    roles=(Role("notes/user", frozenset({READ}), people=True),),
    default="notes/user",
)


def component(environ):
    file = Path(environ["ROBINAUTS_DATA_DIR"]) / "notes.db"
    router = APIRouter()

    @asynccontextmanager
    async def lifespan(context):
        with sqlite3.connect(file) as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS notes (text TEXT NOT NULL)")
            connection.execute("INSERT INTO notes VALUES ('kept')")
        yield

    @router.get("/api/notes", dependencies=[permit(READ)])
    async def notes() -> list[str]:
        with sqlite3.connect(file) as connection:
            return [row[0] for row in connection.execute("SELECT text FROM notes")]

    return Component(name="notes", roles=ROLES, router=router, lifespan=lifespan)


def taken(environ):
    table = RoleTable(app="neorc", permissions=frozenset(), roles=())
    return Component(name="neorc", roles=table, router=APIRouter())
'''


def environment_of(folder: Path, components: str) -> dict[str, str]:
    """A local start on a data directory in ``folder``, which also holds the module ``notes``."""
    (folder / "notes.py").write_text(NOTES)
    config = folder / "robinauts.toml"
    config.write_text("")
    environment = {
        **os.environ,
        "ROBINAUTS_CONFIG": str(config),
        "ROBINAUTS_DATA_DIR": str(folder / "data"),
        "ROBINAUTS_COMPONENTS": components,
        "PYTHONPATH": str(folder),
    }
    for variable in ("ROBINAUTS_DATABASE_URL", "ONEBERTH_CONFIG", "ROBINAUTS_FLOWS_DIR"):
        environment.pop(variable, None)
    return environment


def test_an_extra_application_is_served_beside_robinauts_and_neorc(tmp_path: Path) -> None:
    running, url = stack.start_local(environment_of(tmp_path, "notes:component"))
    try:
        stack.wait_until_up(url, running)
        with httpx.Client(base_url=url, timeout=30.0) as client:
            assert client.get("/notes/api/notes").status_code == 401
            stack.signed_in(client, url)
            assert "notes/user" in client.get("/auth/session").json()["roles"]
            assert client.get("/notes/api/notes").json() == ["kept"]
            assert client.get("/notes/openapi.json").json()["paths"].keys() == {"/notes/api/notes"}
            assert client.get("/robinauts/api/conversations").status_code == 200
            assert client.get("/neorc/flows").status_code == 200
    finally:
        running.terminate()
        assert running.wait(timeout=30) == 0
    assert sorted(p.name for p in (tmp_path / "data").glob("*.db")) == [
        "neorc.db",
        "notes.db",
        "oneberth.db",
        "robinauts.db",
    ]


@pytest.mark.parametrize(
    ("components", "refusal"),
    [
        ("notes:missing", "ROBINAUTS_COMPONENTS: notes:missing: notes has no function missing"),
        ("notes", "ROBINAUTS_COMPONENTS: 'notes' is not <module>:<function>"),
        ("notes:taken", "two components are named 'neorc'"),
    ],
)
def test_an_extra_application_that_cannot_be_composed_stops_the_start(
    tmp_path: Path, components: str, refusal: str
) -> None:
    command = [str(stack.ROBINAUTS), "start", "--local", "--port", str(stack.free_port())]
    environment = environment_of(tmp_path, components)
    try:
        started = subprocess.run(command, env=environment, capture_output=True, timeout=60)
    except subprocess.TimeoutExpired:
        raise AssertionError("the server started with an application it cannot compose") from None
    assert started.returncode == 1
    assert refusal in started.stderr.decode()
