# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The app server's notifications, one turn's worth, as the engine contract's events.

Pure: it is handed each notification's method and params, and gives back the engine's events,
so it is tested without a Codex process. The answer's text comes from the agent messages'
deltas; the reasoning from its summary's deltas, or from its raw text when the provider gives
that alone. A tool call is reported when its item starts, since Codex knows its arguments by
then, and its result when the item completes: a shell command, a file change, a call of an MCP
tool, of a tool run in this process, or a web search. Notifications of another thread (a
subagent's) or of another turn are not this turn's, and are left out.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from robinauts.agent_engines.contract.domain import (
    Done,
    Event,
    ReasoningDelta,
    TextDelta,
    ToolCall,
    ToolResult,
)

SHELL = "shell"
APPLY_PATCH = "apply_patch"
WEB_SEARCH = "web_search"
UNFINISHED = "the call did not finish before the turn ended"
AUTH_STATUSES = (401, 403)
_SUMMARY, _RAW = "summary", "raw"


def call_of(item: Mapping[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """The tool's name and arguments of an item that is a tool call; ``None`` for any other."""
    kind = item.get("type")
    if kind == "commandExecution":
        return SHELL, {"command": item.get("command", ""), "cwd": item.get("cwd")}
    if kind == "fileChange":
        changes = [
            {"path": change.get("path"), "kind": (change.get("kind") or {}).get("type")}
            for change in item.get("changes") or []
        ]
        return APPLY_PATCH, {"changes": changes}
    if kind in ("mcpToolCall", "dynamicToolCall"):
        arguments = item.get("arguments")
        if not isinstance(arguments, Mapping):
            arguments = {} if arguments is None else {"arguments": arguments}
        return str(item.get("tool", "")), dict(arguments)
    if kind == "webSearch":
        return WEB_SEARCH, {"query": item.get("query", "")}
    return None


def content_text(content: Any) -> str:
    """Content blocks, an MCP result's or a dynamic tool's, as the model reads them: their text
    parts, joined; anything else as JSON."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts = []
    for block in content:
        if isinstance(block, Mapping) and block.get("type") in ("text", "inputText"):
            parts.append(str(block.get("text", "")))
        else:
            parts.append(json.dumps(block, sort_keys=True))
    return "\n".join(parts)


def result_of(item: Mapping[str, Any]) -> tuple[str, bool, Mapping[str, Any] | None]:
    """A completed tool item's output as the model sees it, whether it failed, and an MCP
    result's structured content."""
    kind = item.get("type")
    status = item.get("status")
    if kind == "commandExecution":
        exit_code = item.get("exitCode")
        failed = status != "completed" or exit_code not in (0, None)
        return str(item.get("aggregatedOutput") or ""), failed, None
    if kind == "fileChange":
        diffs = [str(change.get("diff") or "") for change in item.get("changes") or []]
        return "\n".join(diffs), status != "completed", None
    if kind == "mcpToolCall":
        error = item.get("error")
        result = item.get("result") or {}
        structured = result.get("structuredContent")
        if error:
            return str(error.get("message", "")), True, None
        failed = status == "failed" or bool(result.get("isError"))
        return content_text(result.get("content")), failed, structured or None
    if kind == "dynamicToolCall":
        failed = status == "failed" or item.get("success") is False
        return content_text(item.get("contentItems")), failed, None
    if kind == "webSearch":
        results = item.get("results")
        return ("" if results is None else json.dumps(results, sort_keys=True)), False, None
    return "", False, None


def http_status(error: Mapping[str, Any] | None) -> int | None:
    """The HTTP status a turn's error carries, in its ``codexErrorInfo``, if any."""
    info = (error or {}).get("codexErrorInfo")
    if isinstance(info, Mapping):
        for detail in info.values():
            if isinstance(detail, Mapping) and isinstance(detail.get("httpStatusCode"), int):
                return int(detail["httpStatusCode"])
    return None


@dataclass
class Translation:
    """One turn's notifications in, its events out, and what the end needs."""

    thread: str
    turn: str
    _names: dict[str, str] = field(default_factory=dict)
    """The tool of each call reported and not yet answered, by item id."""
    _streamed: set[str] = field(default_factory=set)
    """The agent messages whose text came as deltas."""
    _reasoning: dict[str, str] = field(default_factory=dict)
    """Which of a reasoning item's texts is shown: its summary, or its raw text."""
    _text: list[str] = field(default_factory=list)
    last_message: str | None = None
    final: str | None = None
    status: str | None = None
    error: Mapping[str, Any] | None = None

    def mine(self, params: Mapping[str, Any]) -> bool:
        if params.get("threadId") != self.thread:
            return False
        turn = params.get("turnId")
        if turn is None and isinstance(params.get("turn"), Mapping):
            turn = params["turn"].get("id")
        return turn == self.turn

    def translate(self, method: str, params: Mapping[str, Any]) -> list[Event]:
        if not self.mine(params):
            return []
        if method == "item/agentMessage/delta":
            text = str(params.get("delta", ""))
            self._streamed.add(str(params.get("itemId")))
            self._text.append(text)
            return [TextDelta(text)] if text else []
        if method in ("item/reasoning/summaryTextDelta", "item/reasoning/textDelta"):
            which = _SUMMARY if method.endswith("summaryTextDelta") else _RAW
            shown = self._reasoning.setdefault(str(params.get("itemId")), which)
            text = str(params.get("delta", ""))
            return [ReasoningDelta(text)] if text and shown == which else []
        if method == "item/reasoning/summaryPartAdded":
            item = str(params.get("itemId"))
            if params.get("summaryIndex", 0) and self._reasoning.get(item) == _SUMMARY:
                return [ReasoningDelta("\n\n")]
            return []
        if method == "item/started":
            return self._started(params.get("item") or {})
        if method == "item/completed":
            return self._completed(params.get("item") or {})
        if method == "error" and not params.get("willRetry"):
            self.error = params.get("error")
            return []
        if method == "turn/completed":
            return self._end(params.get("turn") or {})
        return []

    def _started(self, item: Mapping[str, Any]) -> list[Event]:
        call = call_of(item)
        if call is None:
            return []
        call_id = str(item.get("id"))
        name, arguments = call
        self._names[call_id] = name
        return [ToolCall(call_id, name, arguments)]

    def _completed(self, item: Mapping[str, Any]) -> list[Event]:
        call_id = str(item.get("id"))
        if item.get("type") == "agentMessage":
            text = str(item.get("text") or "")
            self.last_message = text
            if call_id not in self._streamed and text:
                # Some messages come whole, without deltas: their text all at once.
                self._text.append(text)
                return [TextDelta(text)]
            return []
        if call_id not in self._names:
            if call_of(item) is None:
                return []
            # A call whose start this turn never saw: reported now, with its result.
            events = self._started(item)
        else:
            events = []
        output, failed, structured = result_of(item)
        name = self._names.pop(call_id)
        return [*events, ToolResult(call_id, name, output, failed, structured)]

    def _end(self, turn: Mapping[str, Any]) -> list[Event]:
        self.status = str(turn.get("status"))
        if turn.get("error"):
            self.error = turn["error"]
        for item in turn.get("items") or []:
            if isinstance(item, Mapping) and item.get("type") == "agentMessage":
                self.final = str(item.get("text") or "")
        # Every call reported has its result before the end.
        events: list[Event] = [
            ToolResult(call_id, name, UNFINISHED, True) for call_id, name in self._names.items()
        ]
        self._names.clear()
        return events

    @property
    def ended(self) -> bool:
        return self.status is not None

    def text(self) -> str:
        """The answer's text: the turn's final message, else its last, else what streamed."""
        if self.final:
            return self.final
        if self.last_message:
            return self.last_message
        return "".join(self._text)

    def failure(self) -> str | None:
        """Why the turn did not end well, or ``None`` when it did: what the operator reads."""
        if self.status is None:
            return "Codex ended without completing the turn"
        if self.status == "completed":
            return None
        message = str((self.error or {}).get("message") or "")
        return message or f"Codex ended the turn {self.status}"

    def credential_refused(self) -> bool:
        """Whether the error says the provider refused the credential."""
        if self.error is None:
            return False
        info = self.error.get("codexErrorInfo")
        return info == "unauthorized" or http_status(self.error) in AUTH_STATUSES

    def done(self, checkpoint: str) -> Done:
        return Done(self.text(), checkpoint)
