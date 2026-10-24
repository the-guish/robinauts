# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""A tool server's `exclude`: the tools the engines offer the model, from a real MCP server.

The engines' own MCP clients list the tools; only the model is a stand-in, which keeps what
it was asked (``chat_completions.Vendor``).
"""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import Callable
from typing import Any

import httpx2
import pytest
from kataloop_core import MemoryStore
from test_engines_over_chat_completions import SETTINGS

from robinauts.agent_engines.contract.domain import (
    AgentDefinition,
    ToolServerAuth,
    ToolServerConfig,
)
from robinauts.agent_engines.contract.ports import AgentEngine, EngineSettings
from robinauts.agent_engines.kataloop_engine import engine as kataloop_module
from robinauts.agent_engines.kataloop_engine.engine import KataloopEngine
from robinauts.agent_engines.langchain_engine import engine as langchain_module
from robinauts.agent_engines.langchain_engine.engine import LangChainEngine
from robinauts.agent_engines.langchain_engine.memory import InProcessMemory as LangChainMemory
from robinauts.agent_engines.pydantic_ai_engine import engine as pydantic_ai_module
from robinauts.agent_engines.pydantic_ai_engine.engine import PydanticAIEngine
from robinauts.agent_engines.pydantic_ai_engine.memory import InProcessMemory as PydanticAIMemory
from util.aio import asyncio_test
from util.chat_completions import Vendor, finished, said, streamed
from util.mcp_server import mcp_server

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
    return LangChainEngine(settings, LangChainMemory())


def pydantic_ai(vendor: Vendor, settings: EngineSettings, mp: pytest.MonkeyPatch) -> AgentEngine:
    real = pydantic_ai_module.chat_model

    def plugged(*args: Any) -> Any:
        model, model_settings = real(*args)
        vendor.plugged_into(model.client)
        return model, model_settings

    mp.setattr(pydantic_ai_module, "chat_model", plugged)
    return PydanticAIEngine(settings, PydanticAIMemory())


def kataloop(vendor: Vendor, settings: EngineSettings, mp: pytest.MonkeyPatch) -> AgentEngine:
    real = kataloop_module.client_for

    def plugged(*args: Any) -> Any:
        http = httpx2.AsyncClient(transport=httpx2.MockTransport(vendor.answer))
        return real(*args, http_client=http)

    mp.setattr(kataloop_module, "client_for", plugged)
    return KataloopEngine(settings, MemoryStore())


Build = Callable[[Vendor, EngineSettings, pytest.MonkeyPatch], AgentEngine]


@pytest.mark.parametrize(
    "build", [langchain, pydantic_ai, kataloop], ids=["langchain", "pydantic-ai", "kataloop"]
)
@pytest.mark.parametrize(
    ("exclude", "offered"), [((), ["kept", "dropped"]), (("dropped",), ["kept"])]
)
@asyncio_test
async def test_the_model_is_offered_the_server_s_tools_but_the_excluded(
    build: Build, exclude: tuple[str, ...], offered: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    with mcp_server(kept, dropped) as url:
        server = ToolServerConfig("s", url, auth=ToolServerAuth.NONE, exclude=exclude)
        models = dataclasses.replace(SETTINGS.models, tool_servers={"s": server})
        vendor = Vendor(streamed(*said("ok"), *finished()))
        engine = build(vendor, dataclasses.replace(SETTINGS, models=models), monkeypatch)
        await engine.setup()
        session = uuid.uuid4()
        await engine.create(session)
        agent = AgentDefinition("be brief", tools=("s",))
        stream = engine.stream(
            session, agent, "hi", model="m", checkpoint_id=None, timeout_seconds=10.0
        )
        _ = [event async for event in stream]
    assert [tool["function"]["name"] for tool in vendor.body_sent["tools"]] == offered
