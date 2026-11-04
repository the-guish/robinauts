# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The platform of ``util.stack`` over HTTP: ``tests/e2e`` without the browser.

One model server, one ``robinauts`` server and one schema for the whole run, so a test keeps
to conversations it opened itself: the listing holds every other test's too. ``fake_model``
is what the model server answers each test with, and a test module overrides it; ``local_gpt``
is the model server, holding only that test's requests. The ``<engine>_tools`` agents call
``shout`` on a real MCP server. The interface is a one-line page, so ``/ui/`` is served
whether or not this checkout has built the frontend. ``database_url`` is the schema that
server reads, for a test that has to plant a record the API cannot make for it.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from util import stack
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
def server_config(
    model_server: FakeLocalGPTServer, tools_url: str, tmp_path_factory: pytest.TempPathFactory
) -> Path:
    here = tmp_path_factory.mktemp("api")
    config = here / "robinauts.toml"
    config.write_text(stack.config_for(model_server.base_url, tools_url))
    ui = here / "ui"
    ui.mkdir()
    (ui / "index.html").write_text("<!doctype html><title>Robinauts</title>")
    return config


@pytest.fixture(scope="session")
def database_url(server_config: Path) -> Iterator[str]:
    """The schema the shared server reads, for a test that plants a record of its own."""
    with stack.database(server_config) as url:
        yield url


@pytest.fixture(scope="session")
def server(server_config: Path, database_url: str) -> Iterator[str]:
    env = {**stack.API_KEY, "ROBINAUTS_UI_DIR": str(server_config.parent / "ui")}
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
def api(server: str, local_gpt: FakeLocalGPTServer) -> Iterator[httpx.Client]:
    with httpx.Client(base_url=server, timeout=30.0) as client:
        yield client
