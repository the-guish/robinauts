# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Both engines over OpenAI's Chat Completions: real client and framework, in-process.

Every test runs once per engine (the engine is in the test's id). Each engine's ``plug_*``
function builds the engine with the test's vendor plugged into its client and ``add`` as its
one tool; everything after that is the same for both.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

import pytest
from pydantic_ai.toolsets import FunctionToolset

from aio import asyncio_test
from chat_completions import PATH, Vendor, calling, finished, said, streamed
from contracts.engine import AGENT, ANSWER, ARGUMENTS, CALL_ID, add
from engine_settings import Keys, NoSecrets
from robinauts.agent_engines.contract.domain import (
    Done,
    Event,
    ModelConfig,
    ModelProviderConfig,
    ModelsConfig,
    ProviderKind,
    TextDelta,
    ToolCall,
    ToolResult,
)
from robinauts.agent_engines.contract.ports import AgentEngine, EngineSettings
from robinauts.agent_engines.langchain_engine import engine as langchain_module
from robinauts.agent_engines.langchain_engine.engine import LangChainEngine
from robinauts.agent_engines.langchain_engine.memory import InProcessMemory as LangChainMemory
from robinauts.agent_engines.pydantic_ai_engine import engine as pydantic_ai_module
from robinauts.agent_engines.pydantic_ai_engine.engine import PydanticAIEngine
from robinauts.agent_engines.pydantic_ai_engine.memory import InProcessMemory as PydanticAIMemory

ENDPOINT = "https://gateway.example.test/v1"
PROMPT = "What are two and three?"

SETTINGS = EngineSettings(
    models=ModelsConfig(
        providers={
            "gw": ModelProviderConfig(
                id="gw", kind=ProviderKind.OPENAI_COMPATIBLE, api_key_env="", base_url=ENDPOINT
            )
        },
        models={"m": ModelConfig(id="m", provider="gw", name="vendor-name", max_output_tokens=321)},
    ),
    keys=Keys(),
    tool_secrets=NoSecrets(),
)


def plug_langchain(vendor: Vendor, monkeypatch: pytest.MonkeyPatch) -> AgentEngine:
    real_chat_model = langchain_module.chat_model

    def plugged_chat_model(*args: Any) -> Any:
        model = real_chat_model(*args)
        vendor.plugged_into(model.root_async_client)
        return model

    async def tools_for(*_: object) -> list[Any]:
        return [add]

    monkeypatch.setattr(langchain_module, "chat_model", plugged_chat_model)
    monkeypatch.setattr(langchain_module, "tools_for", tools_for)
    return LangChainEngine(SETTINGS, LangChainMemory())


def plug_pydantic_ai(vendor: Vendor, monkeypatch: pytest.MonkeyPatch) -> AgentEngine:
    real_chat_model = pydantic_ai_module.chat_model

    def plugged_chat_model(*args: Any) -> Any:
        model, model_settings = real_chat_model(*args)
        vendor.plugged_into(model.client)
        return model, model_settings

    def toolsets_for(*_: object) -> list[Any]:
        return [FunctionToolset([add])]

    monkeypatch.setattr(pydantic_ai_module, "chat_model", plugged_chat_model)
    monkeypatch.setattr(pydantic_ai_module, "toolsets_for", toolsets_for)
    return PydanticAIEngine(SETTINGS, PydanticAIMemory())


Plug = Callable[[Vendor, pytest.MonkeyPatch], AgentEngine]

ENGINES = pytest.mark.parametrize(
    "plug", [plug_langchain, plug_pydantic_ai], ids=["langchain", "pydantic-ai"]
)


async def turn_over(plug: Plug, vendor: Vendor, monkeypatch: pytest.MonkeyPatch) -> list[Event]:
    engine = plug(vendor, monkeypatch)
    await engine.setup()
    session = uuid.uuid4()
    await engine.create(session)
    stream = engine.stream(
        session, AGENT, PROMPT, model="m", checkpoint_id=None, timeout_seconds=10.0
    )
    return [event async for event in stream]


@ENGINES
@asyncio_test
async def test_an_answer_is_asked_for_as_configured_and_streamed_back(
    plug: Plug, monkeypatch: pytest.MonkeyPatch
) -> None:
    vendor = Vendor(streamed(*said("Two and three ", "make five."), *finished()))
    events = await turn_over(plug, vendor, monkeypatch)

    pieces = [event.text for event in events if isinstance(event, TextDelta)]
    assert pieces == ["Two and three ", "make five."]
    assert events[-1] == Done(text=ANSWER, checkpoint_id=events[-1].checkpoint_id)
    request = vendor.request
    assert str(request.url) == ENDPOINT + PATH
    assert request.headers["Authorization"] == "Bearer key-of-gw"
    body = vendor.body_sent
    assert (body["model"], body["stream"], body["max_completion_tokens"]) == (
        "vendor-name",
        True,
        321,
    )
    assert [(message["role"], message["content"]) for message in body["messages"]] == [
        ("system", AGENT.system_prompt),
        ("user", PROMPT),
    ]


@ENGINES
@asyncio_test
async def test_a_tool_round_is_run_and_its_result_sent_back(
    plug: Plug, monkeypatch: pytest.MonkeyPatch
) -> None:
    vendor = Vendor(
        bodies=[
            streamed(*calling(CALL_ID, "add", ARGUMENTS), *finished("tool_calls")),
            streamed(*said(ANSWER), *finished()),
        ]
    )
    events = await turn_over(plug, vendor, monkeypatch)

    assert events.index(ToolCall(CALL_ID, "add", ARGUMENTS)) < events.index(
        ToolResult(CALL_ID, "add", "5")
    )
    assert events[-1] == Done(text=ANSWER, checkpoint_id=events[-1].checkpoint_id)
    first, second = vendor.sent
    assert [tool["function"]["name"] for tool in Vendor.body_of(first)["tools"]] == ["add"]
    assert Vendor.body_of(second)["messages"][-1] == {
        "role": "tool",
        "content": "5",
        "tool_call_id": CALL_ID,
    }


@ENGINES
@asyncio_test
async def test_a_run_makes_no_more_model_calls_than_it_is_allowed(
    plug: Plug, monkeypatch: pytest.MonkeyPatch
) -> None:
    vendor = Vendor(
        bodies=[
            streamed(*calling(CALL_ID, "add", ARGUMENTS), *finished("tool_calls")),
            streamed(*said(ANSWER), *finished()),
        ]
    )
    engine = plug(vendor, monkeypatch)
    await engine.setup()
    session = uuid.uuid4()
    await engine.create(session)
    stream = engine.stream(
        session,
        AGENT,
        PROMPT,
        model="m",
        checkpoint_id=None,
        timeout_seconds=10.0,
        max_model_calls=1,
    )
    with pytest.raises(Exception, match="(?i)limit"):
        async for _ in stream:
            pass
    assert len(vendor.sent) == 1
