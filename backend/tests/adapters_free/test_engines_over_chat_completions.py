# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The engines over OpenAI's Chat Completions: real client and framework, in-process.

Every test runs once per engine (the engine is in the test's id). Each engine's ``plug_*``
function builds the engine with the test's vendor plugged into its client and ``add`` as its
one tool; everything after that is the same for all three, but the field the output limit is
sent in: OpenAI's SDK sends ``max_completion_tokens``, and uni-llm-client, kataloop's client,
the older ``max_tokens`` every compatible server knows.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

import httpx2
import pytest
from kataloop_core import LocalToolbox, MemoryStore, ToolServer, ToolSpec
from pydantic_ai.toolsets import FunctionToolset

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
from robinauts.agent_engines.kataloop_engine import engine as kataloop_module
from robinauts.agent_engines.kataloop_engine.engine import KataloopEngine
from robinauts.agent_engines.langchain_engine import engine as langchain_module
from robinauts.agent_engines.langchain_engine.engine import LangChainEngine
from robinauts.agent_engines.langchain_engine.memory import InProcessMemory as LangChainMemory
from robinauts.agent_engines.pydantic_ai_engine import engine as pydantic_ai_module
from robinauts.agent_engines.pydantic_ai_engine.engine import PydanticAIEngine
from robinauts.agent_engines.pydantic_ai_engine.memory import InProcessMemory as PydanticAIMemory
from util.aio import asyncio_test
from util.chat_completions import PATH, Vendor, calling, finished, said, streamed
from util.contracts.engine import AGENT, ANSWER, ARGUMENTS, CALL_ID, add
from util.engine_settings import Keys, NoSecrets

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


def plug_kataloop(vendor: Vendor, monkeypatch: pytest.MonkeyPatch) -> AgentEngine:
    """uni-llm-client's own HTTP client is the seam: it is handed one that the vendor answers."""
    real_client_for = kataloop_module.client_for

    def plugged_client_for(*args: Any) -> Any:
        http = httpx2.AsyncClient(transport=httpx2.MockTransport(vendor.answer))
        return real_client_for(*args, http_client=http)

    def tools_for(*_: object) -> Any:
        return (ToolServer("calculator", "local"),), LocalToolbox({"calculator": [(ADD, run_add)]})

    monkeypatch.setattr(kataloop_module, "client_for", plugged_client_for)
    monkeypatch.setattr(kataloop_module, "tools_for", tools_for)
    return KataloopEngine(SETTINGS, MemoryStore())


ADD = ToolSpec(
    "add",
    "Add two integers.",
    {
        "type": "object",
        "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
        "required": ["a", "b"],
    },
)
"""``add``, as kataloop's in-process toolbox offers it."""


async def run_add(arguments: Any) -> str:
    return str(add(**arguments))


Plug = Callable[[Vendor, pytest.MonkeyPatch], AgentEngine]

ENGINES = pytest.mark.parametrize(
    ("plug", "ceiling"),
    [
        (plug_langchain, "max_completion_tokens"),
        (plug_pydantic_ai, "max_completion_tokens"),
        (plug_kataloop, "max_tokens"),
    ],
    ids=["langchain", "pydantic-ai", "kataloop"],
)
"""Each engine, and the field of the request its output limit is sent in."""


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
    plug: Plug, ceiling: str, monkeypatch: pytest.MonkeyPatch
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
    assert (body["model"], body["stream"], body[ceiling]) == ("vendor-name", True, 321)
    assert {"max_tokens", "max_completion_tokens"} & set(body) == {ceiling}
    assert [(message["role"], message["content"]) for message in body["messages"]] == [
        ("system", AGENT.system_prompt),
        ("user", PROMPT),
    ]


@ENGINES
@asyncio_test
async def test_a_tool_round_is_run_and_its_result_sent_back(
    plug: Plug, ceiling: str, monkeypatch: pytest.MonkeyPatch
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
