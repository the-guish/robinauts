# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The LangChain engine builds its chat models from the settings, and nothing leaves."""

from __future__ import annotations

import asyncio
import itertools
import json
import re
from collections.abc import AsyncIterator
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
    ModelConfig,
    ModelProviderConfig,
    ModelsConfig,
    ProviderKind,
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
from robinauts.agent_engines.langchain_engine import engine as engine_module
from robinauts.agent_engines.langchain_engine.clients import chat_model
from robinauts.agent_engines.langchain_engine.engine import LangChainEngine


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
    model = chat_model("m", settings_for(kind, base_url))
    assert isinstance(model, ChatAnthropic)
    assert model.model == "vendor-name"
    assert model.anthropic_api_key.get_secret_value() == "key-of-p"
    assert model.anthropic_api_url == endpoint
    assert (model.max_retries, model.default_request_timeout, model.max_tokens) == (0, 7.0, 321)


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
    assert (model.max_retries, model.request_timeout, model.max_tokens) == (0, 7.0, 321)


def test_a_model_not_in_the_settings_is_refused() -> None:
    with pytest.raises(UnknownModelError):
        chat_model("other", settings_for(ProviderKind.ANTHROPIC))


def test_the_engine_answers_the_four_kinds_and_turns_hosted_tracing_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    engine = LangChainEngine(
        settings_for(ProviderKind.ANTHROPIC), StorageConfig(StorageKind.IN_MEMORY, {})
    )
    assert engine.kinds() == frozenset(ProviderKind)
    assert not langsmith.utils.tracing_is_enabled()


class TestLangChainEngineMemory(EngineMemoryContract):
    @pytest.fixture(autouse=True)
    def scripted_model(self, monkeypatch: pytest.MonkeyPatch) -> None:
        model = GenericFakeChatModel(messages=itertools.repeat("An answer."))
        monkeypatch.setattr(engine_module, "chat_model", lambda *_: model)

    async def new_engine(self) -> AgentEngine:
        engine = LangChainEngine(
            settings_for(ProviderKind.ANTHROPIC), StorageConfig(StorageKind.IN_MEMORY, {})
        )
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
        if self.script is Script.TOOL_ROUND and not isinstance(messages[-1], ToolMessage):
            call = tool_call_chunk(name="add", args=json.dumps(ARGUMENTS), id=CALL_ID, index=0)
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
        engine = LangChainEngine(
            settings_for(ProviderKind.ANTHROPIC), StorageConfig(StorageKind.IN_MEMORY, {})
        )
        await engine.setup()
        return engine
