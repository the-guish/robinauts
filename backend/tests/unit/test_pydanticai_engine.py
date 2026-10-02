# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The new Pydantic AI engine builds its models from the settings, and nothing leaves."""

from __future__ import annotations

import asyncio
import base64
import json
import re
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import pydantic_ai
import pytest
from anthropic import AsyncAnthropic
from openai import AsyncOpenAI
from pydantic_ai import Agent
from pydantic_ai.mcp import MCPToolset
from pydantic_ai.messages import ModelMessage, ModelRequest, ToolReturnPart
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.toolsets import FunctionToolset

from aio import asyncio_test
from contracts.engine import (
    ANSWER,
    ARGUMENTS,
    CALL_ID,
    EngineMemoryContract,
    EngineTurnContract,
    ModelFailure,
    Script,
    add,
)
from robinauts.agent_engines.contract.domain import (
    AgentDefinition,
    ModelConfig,
    ModelProviderConfig,
    ModelsConfig,
    ProviderKind,
    ToolServerAuth,
    ToolServerConfig,
    UnknownModelError,
)
from robinauts.agent_engines.contract.ports import (
    AgentEngine,
    EngineSettings,
    ProviderKeyLookup,
    StorageConfig,
    StorageKind,
    ToolSecretLookup,
)
from robinauts.agent_engines.pydantic_ai_engine import engine as engine_module
from robinauts.agent_engines.pydantic_ai_engine import init_pydantic_ai
from robinauts.agent_engines.pydantic_ai_engine.clients import chat_model
from robinauts.agent_engines.pydantic_ai_engine.engine import PydanticAIEngine
from robinauts.agent_engines.pydantic_ai_engine.memory import InProcessMemory
from robinauts.agent_engines.pydantic_ai_engine.tools import toolset_for, toolsets_for


class Keys(ProviderKeyLookup):
    def key_for(self, provider_id: str) -> str:
        return f"key-of-{provider_id}"


class NoSecrets(ToolSecretLookup):
    def secret_for(self, server_id: str) -> str:
        raise AssertionError(server_id)


def settings_for(kind: ProviderKind, base_url: str | None = None) -> EngineSettings:
    provider = ModelProviderConfig(id="p", kind=kind, api_key_env="", base_url=base_url)
    model = ModelConfig(
        id="m", provider="p", name="vendor-name", timeout_seconds=7.0, max_output_tokens=321
    )
    return EngineSettings(
        models=ModelsConfig(providers={"p": provider}, models={"m": model}),
        keys=Keys(),
        tool_secrets=NoSecrets(),
    )


@pytest.mark.parametrize(
    ("kind", "base_url", "endpoint"),
    [
        (ProviderKind.ANTHROPIC, None, "https://api.anthropic.com"),
        (ProviderKind.ANTHROPIC_COMPATIBLE, "https://gw.example/api", "https://gw.example/api"),
    ],
)
def test_an_anthropic_model_is_built_from_the_settings(
    kind: ProviderKind, base_url: str | None, endpoint: str
) -> None:
    model, model_settings = chat_model("m", settings_for(kind, base_url))
    assert isinstance(model, AnthropicModel)
    assert (model.model_name, model.system) == ("vendor-name", "anthropic")
    client = model.client
    assert isinstance(client, AsyncAnthropic)
    assert client.api_key == "key-of-p"
    assert str(client.base_url).rstrip("/") == endpoint
    assert (client.max_retries, client.timeout) == (0, 7.0)
    assert model_settings == {"timeout": 7.0, "max_tokens": 321}


@pytest.mark.parametrize(
    ("kind", "base_url", "endpoint"),
    [
        (ProviderKind.OPENAI, None, "https://api.openai.com/v1"),
        (ProviderKind.OPENAI_COMPATIBLE, "https://gw.example/v1", "https://gw.example/v1"),
    ],
)
def test_an_openai_model_is_built_from_the_settings(
    kind: ProviderKind, base_url: str | None, endpoint: str
) -> None:
    model, model_settings = chat_model("m", settings_for(kind, base_url))
    assert isinstance(model, OpenAIChatModel)
    assert (model.model_name, model.system) == ("vendor-name", "openai")
    client = model.client
    assert isinstance(client, AsyncOpenAI)
    assert client.api_key == "key-of-p"
    assert str(client.base_url).rstrip("/") == endpoint
    assert (client.max_retries, client.timeout) == (0, 7.0)
    assert model_settings == {"timeout": 7.0, "max_tokens": 321}


def test_a_model_not_in_the_settings_is_refused() -> None:
    with pytest.raises(UnknownModelError):
        chat_model("other", settings_for(ProviderKind.ANTHROPIC))


class FixedSecret(ToolSecretLookup):
    def __init__(self) -> None:
        self.asked: list[str] = []

    def secret_for(self, server_id: str) -> str:
        self.asked.append(server_id)
        return "s3cret"


def tool_settings(secrets: ToolSecretLookup, *servers: ToolServerConfig) -> EngineSettings:
    return EngineSettings(
        models=ModelsConfig(tool_servers={server.id: server for server in servers}),
        keys=Keys(),
        tool_secrets=secrets,
    )


# The toolsets are never entered: these read what the client would send, and nothing connects.
def test_a_bearer_server_is_reached_with_its_secret_as_a_bearer_token() -> None:
    secrets = FixedSecret()
    server = ToolServerConfig(id="gh", url="https://mcp.example/gh", timeout_seconds=9.0)
    client = toolset_for(server, tool_settings(secrets)).client
    assert client.transport.url == "https://mcp.example/gh"
    assert client.transport.headers == {"Authorization": "Bearer s3cret"}
    assert client._init_timeout == 9.0
    assert client._session_kwargs["read_timeout_seconds"] == timedelta(seconds=9)
    assert secrets.asked == ["gh"]


def test_a_basic_server_is_reached_with_its_user_and_secret() -> None:
    secrets = FixedSecret()
    server = ToolServerConfig(
        id="wiki", url="https://mcp.example/wiki", auth=ToolServerAuth.BASIC, user="ana"
    )
    transport = toolset_for(server, tool_settings(secrets)).client.transport
    pair = base64.b64encode(b"ana:s3cret").decode("ascii")
    assert transport.headers == {"Authorization": f"Basic {pair}"}
    assert secrets.asked == ["wiki"]


def test_a_public_server_carries_no_credential_and_asks_for_none() -> None:
    server = ToolServerConfig(id="docs", url="https://mcp.example/docs", auth=ToolServerAuth.NONE)
    assert toolset_for(server, tool_settings(NoSecrets())).client.transport.headers == {}


@asyncio_test
async def test_an_agent_without_tools_has_none() -> None:
    assert await toolsets_for(AgentDefinition("be brief"), tool_settings(NoSecrets())) == []


@asyncio_test
async def test_an_agent_has_a_toolset_per_server_it_names() -> None:
    server = ToolServerConfig(id="docs", url="https://mcp.example/docs", auth=ToolServerAuth.NONE)
    other = ToolServerConfig(id="gh", url="https://mcp.example/gh")
    settings = tool_settings(NoSecrets(), server, other)
    [toolset] = await toolsets_for(AgentDefinition("be brief", tools=("docs",)), settings)
    assert isinstance(toolset, MCPToolset)
    assert toolset.client.transport.url == "https://mcp.example/docs"


def test_the_engine_answers_the_four_kinds_and_turns_tracing_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Agent, "_instrument_default", True)
    monkeypatch.setattr(pydantic_ai, "BANNER_ENABLED", True)
    engine = PydanticAIEngine(settings_for(ProviderKind.ANTHROPIC), InProcessMemory())
    assert engine.kinds() == frozenset(ProviderKind)
    assert Agent._instrument_default is False


@asyncio_test
async def test_init_pydantic_ai_keeps_memory_in_this_process_without_postgres() -> None:
    engine = await init_pydantic_ai(
        settings_for(ProviderKind.ANTHROPIC), StorageConfig(StorageKind.IN_MEMORY, {})
    )
    assert isinstance(engine, PydanticAIEngine)
    assert isinstance(engine._memory, InProcessMemory)
    assert pydantic_ai.BANNER_ENABLED is False


class TestPydanticAIEngineMemory(EngineMemoryContract):
    @pytest.fixture(autouse=True)
    def scripted_model(self, monkeypatch: pytest.MonkeyPatch) -> None:
        model = TestModel(custom_output_text="An answer.")
        monkeypatch.setattr(engine_module, "chat_model", lambda *_: (model, {}))

    async def new_engine(self) -> AgentEngine:
        engine = PydanticAIEngine(settings_for(ProviderKind.ANTHROPIC), InProcessMemory())
        await engine.setup()
        return engine


def scripted(script: Script) -> FunctionModel:
    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[Any]:
        if script is Script.FAIL:
            raise ModelFailure()
        if script is Script.HANG:
            await asyncio.sleep(3600)
        last = messages[-1]
        answered = isinstance(last, ModelRequest) and isinstance(last.parts[-1], ToolReturnPart)
        if script is Script.TOOL_ROUND and not answered:
            yield {
                0: DeltaToolCall(name="add", json_args=json.dumps(ARGUMENTS), tool_call_id=CALL_ID)
            }
            return
        for piece in re.split(r"(\s)", ANSWER):
            yield piece

    return FunctionModel(stream_function=stream)


class TestPydanticAIEngineTurn(EngineTurnContract):
    @pytest.fixture(autouse=True)
    def plain_tool(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def toolsets_for(*_: object) -> list[Any]:
            return [FunctionToolset([add])]

        monkeypatch.setattr(engine_module, "toolsets_for", toolsets_for)
        self.monkeypatch = monkeypatch

    async def new_engine(self, script: Script) -> AgentEngine:
        model = scripted(script)
        self.monkeypatch.setattr(engine_module, "chat_model", lambda *_: (model, {}))
        engine = PydanticAIEngine(settings_for(ProviderKind.ANTHROPIC), InProcessMemory())
        await engine.setup()
        return engine
