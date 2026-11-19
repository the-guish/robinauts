# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The platform of ``util.stack`` over HTTP: ``tests/e2e`` without the browser.

One model server, one ``robinauts`` server and one database for the whole run, so a test keeps
to conversations it opened itself: the listing holds every other test's too. The server runs in
oneberth's local mode, and ``api`` signs its one user in. ``fake_model``
is what the model server answers each test with, and a test module overrides it; ``local_gpt``
is the model server, holding only that test's requests. The ``<engine>_tools`` agents call
``shout`` on a real MCP server. The interface is a one-line page, so ``/ui/`` is served
whether or not this checkout has built the frontend. ``database_url`` is the database that
server reads, for a test that has to plant a record the API cannot make for it.

A module that needs agents, models or tool servers of its own adds them to this server
(``util.shared_server``) rather than starting a server of its own. Under ``pytest-xdist``
each worker has one of these, on a database of its own in the same PostgreSQL.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from util import shared_server, stack
from util.fake_openai import EchoModel, FakeLocalGPTServer, FakeModel
from util.mcp_server import mcp_server


def shout(text: str) -> str:
    """The text, in capitals."""
    return text.upper()


@pytest.fixture(scope="session")
def model_server() -> Iterator[FakeLocalGPTServer]:
    with FakeLocalGPTServer(EchoModel()) as running:
        yield running


@pytest.fixture(scope="session")
def tools_url() -> Iterator[str]:
    with mcp_server(shout) as url:
        yield url


@pytest.fixture(scope="session")
def contributions() -> Iterator[shared_server.Contribution]:
    """What the test modules add to the shared server, with what it needs running."""
    with shared_server.contributions() as added:
        yield added


@pytest.fixture(scope="session")
def server_config(
    model_server: FakeLocalGPTServer,
    tools_url: str,
    contributions: shared_server.Contribution,
    tmp_path_factory: pytest.TempPathFactory,
) -> Path:
    here = tmp_path_factory.mktemp("api")
    config = here / "robinauts.toml"
    base = stack.config_for(model_server.base_url, tools_url)
    config.write_text(base + contributions.config)
    ui = here / "ui"
    ui.mkdir()
    (ui / "index.html").write_text("<!doctype html><title>Robinauts</title>")
    return config


@pytest.fixture(scope="session")
def database_url(server_config: Path) -> Iterator[str]:
    """The database the shared server reads, for a test that plants a record of its own."""
    with stack.database(server_config) as url:
        yield url


@pytest.fixture(scope="session")
def server(
    server_config: Path, database_url: str, contributions: shared_server.Contribution
) -> Iterator[stack.Server]:
    env = {
        **stack.API_KEY,
        **contributions.env,
        "ROBINAUTS_UI_DIR": str(server_config.parent / "ui"),
    }
    with stack.server(server_config, database_url, env=env) as running:
        yield running


@pytest.fixture
def fake_model() -> FakeModel:
    return EchoModel()


@pytest.fixture
def local_gpt(model_server: FakeLocalGPTServer, fake_model: FakeModel) -> FakeLocalGPTServer:
    model_server.model = fake_model
    model_server.received.clear()
    return model_server


@pytest.fixture
def api(server: stack.Server, local_gpt: FakeLocalGPTServer) -> Iterator[httpx.Client]:
    """A client of the shared server, the local user signed in."""
    with httpx.Client(base_url=server, timeout=30.0) as client:
        yield stack.signed_in(client, server)
