# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The Claude Agent SDK's messages, one turn's worth, as the engine contract's events.

Pure: it is handed the SDK's dataclasses and gives back the engine's, so it is tested without a
Claude Code process. With partial messages on, the answer's text comes as the model writes it,
from the stream's deltas, and the text blocks of the assistant messages that follow are the same
text again, so they are skipped; off, the blocks are the text. Tool calls come from the assistant
messages, tool results from the user messages that carry them, and the end from the result. A
subagent's messages carry ``parent_tool_use_id``: the ``Agent`` call that ran it, and its result,
are shown, and what happened inside it is not.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    StreamEvent,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from robinauts.agent_engines.contract.domain import (
    Done,
    Event,
    ReasoningDelta,
    TextDelta,
    ToolCall,
    ToolResult,
)

REFUSAL = "refusal"
UNKNOWN_TOOL = "?"
"""The name of a tool whose call this turn never saw, which a result names all the same."""


def result_text(content: str | list[dict[str, Any]] | None) -> str:
    """A tool result's content as the model reads it: its text parts, joined; anything else
    as JSON."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts = []
    for item in content:
        if isinstance(item, Mapping) and item.get("type") == "text":
            parts.append(str(item.get("text", "")))
        else:
            parts.append(json.dumps(item, sort_keys=True))
    return "\n".join(parts)


class Translation:
    """One turn's messages in, its events out, and what the end needs: the last assistant
    message's uuid, the result, and the text, in case the result carries none."""

    def __init__(self, *, partial: bool) -> None:
        self._partial = partial
        self._names: dict[str, str] = {}
        self._text: list[str] = []
        self.last_assistant_uuid: str | None = None
        self.result: ResultMessage | None = None

    def translate(self, message: object) -> list[Event]:
        if getattr(message, "parent_tool_use_id", None):
            return []
        if isinstance(message, StreamEvent):
            return self._stream(message.event)
        if isinstance(message, AssistantMessage):
            return self._assistant(message)
        if isinstance(message, UserMessage):
            return self._user(message)
        if isinstance(message, ResultMessage):
            self.result = message
        return []

    def _stream(self, event: Mapping[str, Any]) -> list[Event]:
        if not self._partial or event.get("type") != "content_block_delta":
            return []
        delta = event.get("delta") or {}
        kind = delta.get("type")
        if kind == "text_delta":
            text = str(delta.get("text", ""))
            self._text.append(text)
            return [TextDelta(text)] if text else []
        if kind == "thinking_delta":
            text = str(delta.get("thinking", ""))
            return [ReasoningDelta(text)] if text else []
        return []

    def _assistant(self, message: AssistantMessage) -> list[Event]:
        if message.uuid:
            self.last_assistant_uuid = message.uuid
        events: list[Event] = []
        for block in message.content:
            if isinstance(block, ToolUseBlock):
                self._names[block.id] = block.name
                events.append(ToolCall(block.id, block.name, dict(block.input)))
            elif self._partial:
                continue
            elif isinstance(block, TextBlock):
                if block.text:
                    self._text.append(block.text)
                    events.append(TextDelta(block.text))
            elif isinstance(block, ThinkingBlock) and block.thinking:
                events.append(ReasoningDelta(block.thinking))
        return events

    def _user(self, message: UserMessage) -> list[Event]:
        content = message.content
        if isinstance(content, str):
            return []
        events: list[Event] = []
        for block in content:
            if isinstance(block, ToolResultBlock):
                events.append(
                    ToolResult(
                        block.tool_use_id,
                        self._names.get(block.tool_use_id, UNKNOWN_TOOL),
                        result_text(block.content),
                        bool(block.is_error),
                    )
                )
        return events

    def text(self) -> str:
        """The answer's text: the result's when it has one, else what streamed."""
        if self.result is not None and self.result.result:
            return self.result.result
        return "".join(self._text)

    def failure(self) -> str | None:
        """Why the turn did not end well, or ``None`` when it did: what the operator reads."""
        result = self.result
        if result is None:
            return "Claude Code ended without a result"
        if result.stop_reason == REFUSAL:
            return "the model declined to answer"
        if result.is_error or result.subtype != "success":
            said = "; ".join(result.errors or [])
            return said or result.result or f"Claude Code ended the turn with {result.subtype}"
        return None

    def credential_refused(self) -> bool:
        """Whether the result says Anthropic refused the credential."""
        result = self.result
        return result is not None and result.api_error_status in (401, 403)

    def done(self, checkpoint: str) -> Done:
        return Done(self.text(), checkpoint)
