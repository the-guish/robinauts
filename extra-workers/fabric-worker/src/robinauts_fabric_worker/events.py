# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""A NeMo Fabric ``RunResult`` of the Claude adapter, as the engine contract's events.

Pure: it is handed the result's mapping and gives back the engine's events, so it is tested
without Fabric or Claude Code. The Claude adapter returns the turn whole, at its end: its
``output.events`` are the Claude Agent SDK's messages, each ``{"type": <class name>, "message":
<the dataclass as a mapping>}``, so a content block is known by its keys alone: ``text`` a text
block, ``thinking`` a thinking block, ``id``/``name``/``input`` a tool call, ``tool_use_id`` a
tool result. A subagent's messages carry ``parent_tool_use_id``: the ``Agent`` call that ran it,
and its result, are shown, and what happened inside it is not.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from robinauts.agent_engines.contract.domain import (
    Event,
    ReasoningDelta,
    TextDelta,
    ToolCall,
    ToolResult,
)

SUCCEEDED = "succeeded"
REFUSAL = "refusal"
UNKNOWN_TOOL = "?"
"""The name of a tool whose call this turn never saw, which a result names all the same."""
REFUSED_STATUSES = (401, 403)


def result_text(content: Any) -> str:
    """A tool result's content as the model reads it: its text parts, joined; anything else
    as JSON."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts = []
    for item in content:
        if isinstance(item, Mapping) and item.get("type", "text") == "text" and "text" in item:
            parts.append(str(item.get("text", "")))
        else:
            parts.append(json.dumps(item, sort_keys=True))
    return "\n".join(parts)


def _output(result: Mapping[str, Any]) -> Mapping[str, Any]:
    output = result.get("output")
    return output if isinstance(output, Mapping) else {}


def translate(result: Mapping[str, Any]) -> tuple[list[Event], str]:
    """The turn's events, in order, and the answer's text: the adapter's ``response``, else the
    text blocks joined."""
    events: list[Event] = []
    names: dict[str, str] = {}
    texts: list[str] = []
    for entry in _output(result).get("events") or ():
        if not isinstance(entry, Mapping):
            continue
        message = entry.get("message")
        if not isinstance(message, Mapping) or message.get("parent_tool_use_id"):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        kind = entry.get("type")
        for block in content:
            if not isinstance(block, Mapping):
                continue
            if kind == "AssistantMessage":
                if {"id", "name", "input"} <= block.keys():
                    names[str(block["id"])] = str(block["name"])
                    arguments = block["input"] if isinstance(block["input"], Mapping) else {}
                    events.append(ToolCall(str(block["id"]), str(block["name"]), dict(arguments)))
                elif "thinking" in block:
                    if block["thinking"]:
                        events.append(ReasoningDelta(str(block["thinking"])))
                elif "text" in block and block["text"]:
                    texts.append(str(block["text"]))
                    events.append(TextDelta(str(block["text"])))
            elif kind == "UserMessage" and "tool_use_id" in block:
                call_id = str(block["tool_use_id"])
                events.append(
                    ToolResult(
                        call_id,
                        names.get(call_id, UNKNOWN_TOOL),
                        result_text(block.get("content")),
                        bool(block.get("is_error")),
                    )
                )
    response = _output(result).get("response")
    text = response if isinstance(response, str) and response else "".join(texts)
    return events, text


def failure(result: Mapping[str, Any]) -> str | None:
    """Why the turn did not end well, or ``None`` when it did: what the operator reads. Never a
    credential, and never what was said."""
    output = _output(result)
    if result.get("status") == SUCCEEDED:
        if output.get("stop_reason") == REFUSAL:
            return "the model declined to answer"
        return None
    error = result.get("error")
    error = error if isinstance(error, Mapping) else {}
    code = error.get("code") or "failed"
    message = error.get("message") or "the Fabric invocation failed"
    subtype = output.get("subtype")
    said = f"{message} ({code})"
    return f"{said}, Claude Code ended with {subtype}" if subtype else said


def credential_refused(result: Mapping[str, Any]) -> bool:
    """Whether the result says Anthropic refused the credential."""
    error = result.get("error")
    metadata = error.get("metadata") if isinstance(error, Mapping) else None
    return isinstance(metadata, Mapping) and metadata.get("api_error_status") in REFUSED_STATUSES


def encode(event: Event) -> dict[str, Any]:
    """An event as the turn log keeps it (history.py)."""
    if isinstance(event, TextDelta):
        return {"kind": "text", "text": event.text}
    if isinstance(event, ReasoningDelta):
        return {"kind": "reasoning", "text": event.text}
    if isinstance(event, ToolCall):
        return {"kind": "call", "id": event.call_id, "name": event.name, "input": event.raw_input}
    if isinstance(event, ToolResult):
        return {
            "kind": "result",
            "id": event.call_id,
            "name": event.name,
            "output": event.raw_output,
            "failed": event.failed,
        }
    raise ValueError(f"not an event the log keeps: {type(event).__name__}")


def decode(data: Mapping[str, Any]) -> Event:
    kind = data.get("kind")
    if kind == "text":
        return TextDelta(data["text"])
    if kind == "reasoning":
        return ReasoningDelta(data["text"])
    if kind == "call":
        return ToolCall(data["id"], data["name"], dict(data["input"]))
    if kind == "result":
        return ToolResult(data["id"], data["name"], data["output"], bool(data["failed"]))
    raise ValueError(f"not an event of the log's: {kind!r}")
