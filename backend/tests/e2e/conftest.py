# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The platform of ``util.stack`` for each test, and a browser on it.

``fake_model`` is what the model server answers with, and ``tools_url`` the MCP server the
``<engine>_tools`` agents use (none by default); a test module overrides either.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from playwright.sync_api import Page

from util import stack
from util.browser import browser_page
from util.fake_openai import EchoModel, FakeLocalGPTServer, FakeModel


@pytest.fixture
def fake_model() -> FakeModel:
    return EchoModel()


@pytest.fixture
def tools_url() -> str | None:
    return None


@pytest.fixture
def local_gpt(fake_model: FakeModel) -> Iterator[FakeLocalGPTServer]:
    with FakeLocalGPTServer(fake_model) as running:
        yield running


@pytest.fixture
def server(local_gpt: FakeLocalGPTServer, tools_url: str | None, tmp_path: Path) -> Iterator[str]:
    """The URL of a real server on the configuration of ``util.stack``, on a database of its own."""
    config = tmp_path / "robinauts.toml"
    config.write_text(stack.config_for(local_gpt.base_url, tools_url))
    with (
        stack.database(config) as url,
        stack.server(config, url, env=stack.API_KEY) as running,
    ):
        yield running


@pytest.fixture
def page() -> Iterator[Page]:
    with browser_page() as page:
        yield page
