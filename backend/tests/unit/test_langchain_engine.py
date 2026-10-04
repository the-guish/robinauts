# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The LangChain engine builds its chat models from the settings, and nothing leaves."""

from __future__ import annotations

import asyncio
import base64
import itertools
import json
import re
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import langsmith.utils
import pytest
from langchain_anthropic import ChatAnthropic
from langchain_core.language_models import BaseChatModel
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessageChunk, BaseMessage, ToolMessage
from langchain_core.messages.tool import tool_call_chunk
from langchain_core.outputs import ChatGenerationChunk, ChatResult
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import InMemorySaver

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
from engine_settings import Keys, NoSecrets, settings_for
from robinauts.agent_engines.contract.domain import (
    AgentDefinition,
    ModelsConfig,
    ProviderKind,
    ToolServerAuth,
    ToolServerConfig,
    UnknownModelError,
)
from robinauts.agent_engines.contract.ports import (
    AgentEngine,
    EngineSettings,
    StorageConfig,
    StorageKind,
    ToolSecretLookup,
)
from robinauts.agent_engines.langchain_engine import engine as engine_module
from robinauts.agent_engines.langchain_engine import init_langchain
from robinauts.agent_engines.langchain_engine.clients import chat_model
from robinauts.agent_engines.langchain_engine.engine import LangChainEngine
from robinauts.agent_engines.langchain_engine.memory import InProcessMemory
from robinauts.agent_engines.langchain_engine.tools import connection_for, tools_for


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
    model = chat_model("m", settings_for(kind, base_url))
    assert isinstance(model, ChatAnthropic)
    assert model.model == "vendor-name"
    assert model.anthropic_api_key.get_secret_value() == "key-of-p"
    assert model.anthropic_api_url == endpoint
    assert (model.max_retries, model.default_request_timeout, model.max_tokens) == (3, 7.0, 321)


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
    model = chat_model("m", settings_for(kind, base_url))
    assert isinstance(model, ChatOpenAI)
    assert model.model_name == "vendor-name"
    assert model.openai_api_key is not None
    assert model.openai_api_key.get_secret_value() == "key-of-p"
    assert model.openai_api_base == endpoint
    assert (model.max_retries, model.request_timeout, model.max_tokens) == (3, 7.0, 321)


def test_a_model_not_in_the_settings_is_refused() -> None:
    settings = settings_for(ProviderKind.ANTHROPIC)
    with pytest.raises(UnknownModelError):
        chat_model("other", settings)


def test_the_engine_answers_the_four_kinds_and_turns_hosted_tracing_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    engine = LangChainEngine(settings_for(ProviderKind.ANTHROPIC), InProcessMemory())
    assert engine.kinds() == frozenset(ProviderKind)
    assert not langsmith.utils.tracing_is_enabled()


def test_init_langchain_keeps_memory_in_this_process_without_postgres() -> None:
    engine = init_langchain(
        settings_for(ProviderKind.ANTHROPIC), StorageConfig(StorageKind.IN_MEMORY, {})
    )
    assert isinstance(engine, LangChainEngine)
    assert isinstance(engine._memory, InProcessMemory)
    assert isinstance(engine._memory.saver, InMemorySaver)


class FixedSecret(ToolSecretLookup):
    def __init__(self) -> None:
        self.asked: list[str] = []

    def secret_for(self, server_id: str) -> str:
        self.asked.append(server_id)
        return "s3cret"


def tool_settings(secrets: ToolSecretLookup) -> EngineSettings:
    return EngineSettings(models=ModelsConfig(), keys=Keys(), tool_secrets=secrets)


def test_a_bearer_server_is_reached_with_its_secret_as_a_bearer_token() -> None:
    secrets = FixedSecret()
    server = ToolServerConfig(id="gh", url="https://mcp.example/gh", timeout_seconds=9.0)
    assert connection_for(server, tool_settings(secrets)) == {
        "transport": "streamable_http",
        "url": "https://mcp.example/gh",
        "headers": {"Authorization": "Bearer s3cret"},
        "timeout": timedelta(seconds=9),
        "sse_read_timeout": timedelta(seconds=9),
    }
    assert secrets.asked == ["gh"]


def test_a_basic_server_is_reached_with_its_user_and_secret() -> None:
    secrets = FixedSecret()
    server = ToolServerConfig(
        id="wiki", url="https://mcp.example/wiki", auth=ToolServerAuth.BASIC, user="ana"
    )
    connection = connection_for(server, tool_settings(secrets))
    pair = base64.b64encode(b"ana:s3cret").decode("ascii")
    assert connection["headers"] == {"Authorization": f"Basic {pair}"}
    assert (connection["transport"], connection["url"]) == (
        "streamable_http",
        "https://mcp.example/wiki",
    )
    assert connection["timeout"] == timedelta(seconds=60)
    assert secrets.asked == ["wiki"]


def test_a_header_server_is_reached_with_its_secret_in_the_header_it_names() -> None:
    secrets = FixedSecret()
    server = ToolServerConfig(
        id="composio",
        url="https://mcp.example/composio",
        auth=ToolServerAuth.HEADER,
        header="x-api-key",
    )
    connection = connection_for(server, tool_settings(secrets))
    assert connection["headers"] == {"x-api-key": "s3cret"}
    assert connection["url"] == "https://mcp.example/composio"
    assert secrets.asked == ["composio"]


def test_a_public_server_carries_no_credential_and_asks_for_none() -> None:
    server = ToolServerConfig(id="docs", url="https://mcp.example/docs", auth=ToolServerAuth.NONE)
    assert connection_for(server, tool_settings(NoSecrets()))["headers"] == {}


@asyncio_test
async def test_an_agent_without_tools_has_none() -> None:
    assert await tools_for(AgentDefinition("be brief"), tool_settings(NoSecrets())) == []


class TestLangChainEngineMemory(EngineMemoryContract):
    @pytest.fixture(autouse=True)
    def scripted_model(self, monkeypatch: pytest.MonkeyPatch) -> None:
        model = GenericFakeChatModel(messages=itertools.repeat("An answer."))
        monkeypatch.setattr(engine_module, "chat_model", lambda *_: model)

    async def new_engine(self) -> AgentEngine:
        engine = LangChainEngine(settings_for(ProviderKind.ANTHROPIC), InProcessMemory())
        await engine.setup()
        return engine


class ScriptedChatModel(BaseChatModel):
    script: Script

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools: Any, **kwargs: Any) -> ScriptedChatModel:
        return self

    def _generate(self, *args: Any, **kwargs: Any) -> ChatResult:
        raise NotImplementedError("the engine streams")

    async def _astream(
        self, messages: list[BaseMessage], *args: Any, **kwargs: Any
    ) -> AsyncIterator[ChatGenerationChunk]:
        if self.script is Script.FAIL:
            raise ModelFailure()
        if self.script is Script.HANG:
            await asyncio.sleep(3600)
        looping = self.script is Script.TOOL_LOOP
        if looping or (
            self.script is Script.TOOL_ROUND and not isinstance(messages[-1], ToolMessage)
        ):
            call_id = f"{CALL_ID}-{len(messages)}" if looping else CALL_ID
            call = tool_call_chunk(name="add", args=json.dumps(ARGUMENTS), id=call_id, index=0)
            yield ChatGenerationChunk(message=AIMessageChunk(content="", tool_call_chunks=[call]))
            return
        for piece in re.split(r"(\s)", ANSWER):
            yield ChatGenerationChunk(message=AIMessageChunk(content=piece))


class TestLangChainEngineTurn(EngineTurnContract):
    @pytest.fixture(autouse=True)
    def plain_tool(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def tools_for(*_: object) -> list[Any]:
            return [add]

        monkeypatch.setattr(engine_module, "tools_for", tools_for)
        self.monkeypatch = monkeypatch

    async def new_engine(self, script: Script) -> AgentEngine:
        model = ScriptedChatModel(script=script)
        self.monkeypatch.setattr(engine_module, "chat_model", lambda *_: model)
        engine = LangChainEngine(settings_for(ProviderKind.ANTHROPIC), InProcessMemory())
        await engine.setup()
        return engine
