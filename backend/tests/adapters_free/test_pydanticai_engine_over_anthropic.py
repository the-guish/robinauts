# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The Pydantic AI engine over Anthropic's Messages protocol: real client and framework, in-process.

A gateway such as OpenRouter serving Gemini answers with an unsigned thinking summary. Pydantic
AI sends an unsigned thinking part back as text in ``<thinking>`` tags, and the model then copies
that form into its answers.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from pydantic_ai.toolsets import FunctionToolset

from robinauts.agent_engines.contract.domain import (
    Done,
    ModelConfig,
    ModelProviderConfig,
    ModelsConfig,
    ProviderKind,
    ReasoningDelta,
)
from robinauts.agent_engines.contract.ports import EngineSettings
from robinauts.agent_engines.pydantic_ai_engine import engine as pydantic_ai_module
from robinauts.agent_engines.pydantic_ai_engine.engine import PydanticAIEngine
from robinauts.agent_engines.pydantic_ai_engine.session_store import InProcessSessionStore
from util.aio import asyncio_test
from util.chat_completions import Vendor
from util.contracts.engine import AGENT, ANSWER, ARGUMENTS, CALL_ID, add
from util.engine_settings import Keys, NoSecrets

SETTINGS = EngineSettings(
    models=ModelsConfig(
        providers={
            "gw": ModelProviderConfig(
                id="gw",
                kind=ProviderKind.ANTHROPIC_COMPATIBLE,
                api_key_env="",
                base_url="https://gateway.example.test/api",
            )
        },
        models={"m": ModelConfig(id="m", provider="gw", name="vendor-name")},
    ),
    keys=Keys(),
    tool_secrets=NoSecrets(),
)

SUMMARY = "**Adding numbers**\n\nI'll call add."


def streamed(blocks: list[tuple[dict[str, Any], list[dict[str, Any]]]], stop: str) -> bytes:
    """A Messages stream: each content block's start and its deltas, then the stop reason."""
    events: list[dict[str, Any]] = [
        {
            "type": "message_start",
            "message": {
                "id": "msg_robinauts",
                "type": "message",
                "role": "assistant",
                "model": "vendor-name",
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 7, "output_tokens": 1},
            },
        }
    ]
    for index, (start, deltas) in enumerate(blocks):
        events.append({"type": "content_block_start", "index": index, "content_block": start})
        events += [{"type": "content_block_delta", "index": index, "delta": d} for d in deltas]
        events.append({"type": "content_block_stop", "index": index})
    events += [
        {
            "type": "message_delta",
            "delta": {"stop_reason": stop, "stop_sequence": None},
            "usage": {"output_tokens": 5},
        },
        {"type": "message_stop"},
    ]
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


def thinking(signature: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    deltas: list[dict[str, Any]] = [{"type": "thinking_delta", "thinking": SUMMARY}]
    if signature:
        deltas.append({"type": "signature_delta", "signature": signature})
    return {"type": "thinking", "thinking": "", "signature": ""}, deltas


REDACTED = ({"type": "redacted_thinking", "data": "encrypted-state"}, [])
TOOL_USE = (
    {"type": "tool_use", "id": CALL_ID, "name": "add", "input": {}},
    [{"type": "input_json_delta", "partial_json": json.dumps(ARGUMENTS)}],
)
TEXT = ({"type": "text", "text": ""}, [{"type": "text_delta", "text": ANSWER}])


@pytest.mark.parametrize(
    ("signature", "replayed"),
    [
        ("", ["redacted_thinking", "tool_use"]),
        ("sig-of-anthropic", ["thinking", "redacted_thinking", "tool_use"]),
    ],
    ids=["unsigned-dropped", "signed-kept"],
)
@asyncio_test
async def test_only_signed_thinking_is_sent_back_after_a_tool_round(
    signature: str, replayed: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    vendor = Vendor(
        bodies=[
            streamed([thinking(signature), REDACTED, TOOL_USE], "tool_use"),
            streamed([TEXT], "end_turn"),
        ]
    )
    real_chat_model = pydantic_ai_module.chat_model

    def plugged_chat_model(*args: Any) -> Any:
        model, model_settings = real_chat_model(*args)
        vendor.plugged_into(model.client)
        return model, model_settings

    monkeypatch.setattr(pydantic_ai_module, "chat_model", plugged_chat_model)
    monkeypatch.setattr(pydantic_ai_module, "toolsets_for", lambda *_: [FunctionToolset([add])])
    engine = PydanticAIEngine(SETTINGS, InProcessSessionStore())
    await engine.setup()
    session = uuid.uuid4()
    await engine.create(session)
    stream = engine.stream(
        session,
        AGENT,
        "What are two and three?",
        model="m",
        checkpoint_id=None,
        timeout_seconds=10.0,
    )
    events = [event async for event in stream]

    assert ReasoningDelta(SUMMARY) in events
    assert events[-1] == Done(text=ANSWER, checkpoint_id=events[-1].checkpoint_id)
    _, second = vendor.sent
    body = Vendor.body_of(second)
    answer = next(m for m in body["messages"] if m["role"] == "assistant")
    assert [block["type"] for block in answer["content"]] == replayed
    assert "<thinking>" not in second.content.decode()
