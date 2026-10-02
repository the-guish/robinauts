# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The Pydantic AI engine over OpenAI's Chat Completions: real client and framework, in-process."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from pydantic_ai.toolsets import FunctionToolset

from aio import asyncio_test
from chat_completions import PATH, Vendor, calling, finished, said, streamed
from contracts.engine import AGENT, ANSWER, ARGUMENTS, CALL_ID, add
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
from robinauts.agent_engines.contract.ports import (
    EngineSettings,
    ProviderKeyLookup,
    ToolSecretLookup,
)
from robinauts.agent_engines.pydantic_ai_engine import engine as engine_module
from robinauts.agent_engines.pydantic_ai_engine.clients import chat_model
from robinauts.agent_engines.pydantic_ai_engine.engine import PydanticAIEngine
from robinauts.agent_engines.pydantic_ai_engine.memory import InProcessMemory

ENDPOINT = "https://gateway.example.test/v1"
PROMPT = "What are two and three?"


class Keys(ProviderKeyLookup):
    def key_for(self, provider_id: str) -> str:
        return f"key-of-{provider_id}"


class NoSecrets(ToolSecretLookup):
    def secret_for(self, server_id: str) -> str:
        raise AssertionError(server_id)


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


async def turn_over(vendor: Vendor, monkeypatch: pytest.MonkeyPatch) -> list[Event]:
    def plugged_chat_model(*args: Any) -> Any:
        model, model_settings = chat_model(*args)
        vendor.plugged_into(model.client)
        return model, model_settings

    async def toolsets_for(*_: object) -> list[Any]:
        return [FunctionToolset([add])]

    monkeypatch.setattr(engine_module, "chat_model", plugged_chat_model)
    monkeypatch.setattr(engine_module, "toolsets_for", toolsets_for)
    engine = PydanticAIEngine(SETTINGS, InProcessMemory())
    await engine.setup()
    session = uuid.uuid4()
    await engine.create(session)
    stream = engine.stream(
        session, AGENT, PROMPT, model="m", checkpoint_id=None, timeout_seconds=10.0
    )
    return [event async for event in stream]


@asyncio_test
async def test_an_answer_is_asked_for_as_configured_and_streamed_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vendor = Vendor(streamed(*said("Two and three ", "make five."), *finished()))
    events = await turn_over(vendor, monkeypatch)

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


@asyncio_test
async def test_a_tool_round_is_run_and_its_result_sent_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vendor = Vendor(
        bodies=[
            streamed(*calling(CALL_ID, "add", ARGUMENTS), *finished("tool_calls")),
            streamed(*said(ANSWER), *finished()),
        ]
    )
    events = await turn_over(vendor, monkeypatch)

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
