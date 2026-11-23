# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The tools both engines offer the model, from a real MCP server: all but those a tool
server's `exclude` names, and those only a view may call (``docs/architecture/mcp-apps.md``).

The engines' own MCP clients list the tools; only the model is a stand-in, which keeps what
it was asked (``chat_completions.Vendor``).
"""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import Callable
from typing import Any

import pytest
from test_engines_over_chat_completions import SETTINGS

from robinauts.agent_engines.contract.domain import (
    AgentDefinition,
    ToolServerAuth,
    ToolServerConfig,
)
from robinauts.agent_engines.contract.ports import AgentEngine, EngineSettings
from robinauts.agent_engines.langchain_engine import engine as langchain_module
from robinauts.agent_engines.langchain_engine.engine import LangChainEngine
from robinauts.agent_engines.langchain_engine.session_store import (
    InProcessSessionStore as LangChainSessionStore,
)
from robinauts.agent_engines.pydantic_ai_engine import engine as pydantic_ai_module
from robinauts.agent_engines.pydantic_ai_engine.engine import PydanticAIEngine
from robinauts.agent_engines.pydantic_ai_engine.session_store import (
    InProcessSessionStore as PydanticAISessionStore,
)
from util.aio import asyncio_test
from util.chat_completions import Vendor, finished, said, streamed
from util.mcp_server import View, mcp_server

pytestmark = pytest.mark.io


def kept(a: int) -> int:
    """A tool the agent is given."""
    return a


def dropped(a: int) -> int:
    """A tool the agent may not be given."""
    return a


def langchain(vendor: Vendor, settings: EngineSettings, mp: pytest.MonkeyPatch) -> AgentEngine:
    real = langchain_module.chat_model

    def plugged(*args: Any) -> Any:
        model = real(*args)
        vendor.plugged_into(model.root_async_client)
        return model

    mp.setattr(langchain_module, "chat_model", plugged)
    return LangChainEngine(settings, LangChainSessionStore())


def pydantic_ai(vendor: Vendor, settings: EngineSettings, mp: pytest.MonkeyPatch) -> AgentEngine:
    real = pydantic_ai_module.chat_model

    def plugged(*args: Any) -> Any:
        model, model_settings = real(*args)
        vendor.plugged_into(model.client)
        return model, model_settings

    mp.setattr(pydantic_ai_module, "chat_model", plugged)
    return PydanticAIEngine(settings, PydanticAISessionStore())


Build = Callable[[Vendor, EngineSettings, pytest.MonkeyPatch], AgentEngine]


async def offered_by(
    build: Build, url: str, exclude: tuple[str, ...], mp: pytest.MonkeyPatch
) -> list[str]:
    """The names of the tools the model is offered in a turn of an agent of the server at
    ``url``."""
    server = ToolServerConfig("s", url, auth=ToolServerAuth.NONE, exclude=exclude)
    models = dataclasses.replace(SETTINGS.models, tool_servers={"s": server})
    vendor = Vendor(streamed(*said("ok"), *finished()))
    engine = build(vendor, dataclasses.replace(SETTINGS, models=models), mp)
    await engine.setup()
    session = uuid.uuid4()
    await engine.create(session)
    agent = AgentDefinition("be brief", tools=("s",))
    stream = engine.stream(
        session, agent, "hi", model="m", checkpoint_id=None, timeout_seconds=10.0
    )
    _ = [event async for event in stream]
    return [tool["function"]["name"] for tool in vendor.body_sent["tools"]]


@pytest.mark.parametrize("build", [langchain, pydantic_ai], ids=["langchain", "pydantic-ai"])
@pytest.mark.parametrize(
    ("exclude", "offered"), [((), ["kept", "dropped"]), (("dropped",), ["kept"])]
)
@asyncio_test
async def test_the_model_is_offered_the_server_s_tools_but_the_excluded(
    build: Build, exclude: tuple[str, ...], offered: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    with mcp_server(kept, dropped) as url:
        assert await offered_by(build, url, exclude, monkeypatch) == offered


def for_views() -> str:
    """A tool only a view calls."""
    return "fresh"


def for_the_model() -> str:
    """A tool only the model calls."""
    return "shown"


def for_both() -> str:
    """A tool the model and a view call."""
    return "changed"


@pytest.mark.parametrize("build", [langchain, pydantic_ai], ids=["langchain", "pydantic-ai"])
@asyncio_test
async def test_the_model_is_not_offered_a_tool_only_a_view_may_call(
    build: Build, monkeypatch: pytest.MonkeyPatch
) -> None:
    views = [
        View(for_views, "<p>a view</p>", visibility=("app",)),
        View(for_the_model, "<p>a view</p>", visibility=("model",)),
        View(for_both, "<p>a view</p>", visibility=("model", "app")),
    ]
    with mcp_server(kept, views=views) as url:
        offered = await offered_by(build, url, (), monkeypatch)
    assert offered == ["kept", "for_the_model", "for_both"]
