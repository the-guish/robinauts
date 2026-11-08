# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

from __future__ import annotations

from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from robinauts.agent_engines.contract.domain import (
    Done,
    ReasoningDelta,
    TextDelta,
    ToolCall,
    ToolResult,
)
from robinauts_claude_worker.events import Translation, result_text

SESSION = "11111111-1111-1111-1111-111111111111"


def delta(kind: str, text: str, parent: str | None = None) -> StreamEvent:
    key = "text" if kind == "text_delta" else "thinking"
    event = {"type": "content_block_delta", "index": 0, "delta": {"type": kind, key: text}}
    return StreamEvent(uuid="e", session_id=SESSION, event=event, parent_tool_use_id=parent)


def result(**fields: object) -> ResultMessage:
    given = {
        "subtype": "success",
        "duration_ms": 1,
        "duration_api_ms": 1,
        "is_error": False,
        "num_turns": 1,
        "session_id": SESSION,
        "total_cost_usd": 0.01,
        "result": None,
    }
    given.update(fields)
    return ResultMessage(**given)  # type: ignore[arg-type]


def test_a_turn_with_a_tool_call_streamed_as_the_model_writes() -> None:
    translation = Translation(partial=True)
    read = ToolUseBlock(id="toolu_1", name="Read", input={"file_path": "/w/a.py"})
    messages = [
        SystemMessage(subtype="init", data={"session_id": SESSION}),
        delta("thinking_delta", "let me look"),
        AssistantMessage(content=[read], model="m", uuid="a-1"),
        UserMessage(content=[ToolResultBlock(tool_use_id="toolu_1", content="print(1)\n")]),
        delta("text_delta", "The file "),
        delta("text_delta", "prints one."),
        AssistantMessage(content=[TextBlock("The file prints one.")], model="m", uuid="a-2"),
        result(result="The file prints one."),
    ]
    events = [e for m in messages for e in translation.translate(m)]
    assert events == [
        ReasoningDelta("let me look"),
        ToolCall("toolu_1", "Read", {"file_path": "/w/a.py"}),
        ToolResult("toolu_1", "Read", "print(1)\n", False),
        TextDelta("The file "),
        TextDelta("prints one."),
    ]
    assert translation.last_assistant_uuid == "a-2"
    assert translation.failure() is None
    assert translation.done("cp") == Done("The file prints one.", "cp")


def test_without_partial_messages_the_blocks_are_the_text() -> None:
    translation = Translation(partial=False)
    messages = [
        delta("text_delta", "streamed, and ignored"),
        AssistantMessage(
            content=[ThinkingBlock("hm", "sig"), TextBlock("Whole answer.")], model="m", uuid="a"
        ),
        result(),
    ]
    events = [e for m in messages for e in translation.translate(m)]
    assert events == [ReasoningDelta("hm"), TextDelta("Whole answer.")]
    # No text on the result: what streamed is the answer.
    assert translation.text() == "Whole answer."


def test_a_subagents_messages_are_not_shown_but_its_call_and_result_are() -> None:
    translation = Translation(partial=True)
    agent = ToolUseBlock(id="toolu_a", name="Agent", input={"prompt": "look around"})
    messages = [
        AssistantMessage(content=[agent], model="m", uuid="a-1"),
        delta("text_delta", "inside", parent="toolu_a"),
        AssistantMessage(
            content=[ToolUseBlock(id="toolu_in", name="Grep", input={})],
            model="m",
            parent_tool_use_id="toolu_a",
            uuid="a-in",
        ),
        UserMessage(
            content=[ToolResultBlock(tool_use_id="toolu_in", content="x")],
            parent_tool_use_id="toolu_a",
        ),
        UserMessage(
            content=[
                ToolResultBlock(
                    tool_use_id="toolu_a",
                    content=[{"type": "text", "text": "found it"}, {"type": "image", "n": 1}],
                )
            ]
        ),
        result(result="done"),
    ]
    events = [e for m in messages for e in translation.translate(m)]
    assert events == [
        ToolCall("toolu_a", "Agent", {"prompt": "look around"}),
        ToolResult("toolu_a", "Agent", 'found it\n{"n": 1, "type": "image"}', False),
    ]
    assert translation.last_assistant_uuid == "a-1"


def test_a_failed_tool_call_and_a_result_the_turn_never_saw() -> None:
    translation = Translation(partial=True)
    events = translation.translate(
        UserMessage(content=[ToolResultBlock(tool_use_id="toolu_x", content="no", is_error=True)])
    )
    assert events == [ToolResult("toolu_x", "?", "no", True)]
    assert result_text(None) == ""


def test_how_a_turn_ends_badly() -> None:
    translation = Translation(partial=True)
    assert translation.failure() == "Claude Code ended without a result"
    translation.translate(result(stop_reason="refusal"))
    assert translation.failure() == "the model declined to answer"
    translation.translate(result(subtype="error_max_turns", is_error=True, errors=["too many"]))
    assert translation.failure() == "too many"
    translation.translate(result(subtype="error_during_execution", is_error=True, errors=[]))
    assert translation.failure() == "Claude Code ended the turn with error_during_execution"
    assert not translation.credential_refused()
    translation.translate(result(is_error=True, errors=["401"], api_error_status=401))
    assert translation.credential_refused()
