# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""OpenCode's server events, one turn's worth, as the engine contract's events.

Pure: it is handed each event's ``type`` and ``properties``, and gives back the engine's events,
so it is tested without an OpenCode process. One turn is one user message and the assistant
messages that answer it, one per step of OpenCode's loop. The answer's text and the reasoning
come as deltas of their parts, or whole when a part ends without any. A tool call is reported
once its part runs, when its input is known, and its result when the part completes or fails.
Events of another session (a subagent's runs in a child session) or of a message from before
the turn are not this turn's, and are left out. The turn ends when the session goes idle after
it started.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
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
from robinauts_opencode_worker.credentials import AUTH_STATUSES

UNFINISHED = "the call did not finish before the turn ended"
SHOWN = ("text", "reasoning")
AUTH_ERROR = "ProviderAuthError"


def _session_of(properties: Mapping[str, Any]) -> str | None:
    if (session := properties.get("sessionID")) is not None:
        return str(session)
    for key in ("part", "info"):
        inner = properties.get(key)
        if isinstance(inner, Mapping) and inner.get("sessionID") is not None:
            return str(inner["sessionID"])
    return None


def error_text(error: Mapping[str, Any] | None) -> str:
    """What a named error of OpenCode's says: its message, else its name."""
    if not error:
        return ""
    data = error.get("data")
    message = data.get("message") if isinstance(data, Mapping) else None
    return str(message or error.get("name") or "")


def refuses_credential(error: Mapping[str, Any] | None) -> bool:
    """Whether a named error says the provider refused the key."""
    if not error:
        return False
    if error.get("name") == AUTH_ERROR:
        return True
    data = error.get("data")
    return isinstance(data, Mapping) and data.get("statusCode") in AUTH_STATUSES


def output_of(state: Mapping[str, Any]) -> str:
    output = state.get("output")
    if output is None:
        return ""
    return output if isinstance(output, str) else json.dumps(output, sort_keys=True)


@dataclass
class Translation:
    """One turn's events in, the engine's out, and what the end needs."""

    session: str
    before: frozenset[str] = frozenset()
    """The ids of the messages the session held before the turn."""
    name: Callable[[str], str] = str
    """A tool's name as the chat shows it, from OpenCode's."""
    _roles: dict[str, str] = field(default_factory=dict)
    _kinds: dict[str, str] = field(default_factory=dict)
    """The kind of each part seen, by id: text, reasoning, tool and the rest."""
    _deltas: set[str] = field(default_factory=set)
    """The parts whose text came as deltas."""
    _calls: dict[str, str] = field(default_factory=dict)
    """The tool of each call reported and not yet answered, by call id."""
    _answered: set[str] = field(default_factory=set)
    _text: list[str] = field(default_factory=list)
    user_message: str | None = None
    started: bool = False
    ended: bool = False
    error: Mapping[str, Any] | None = None

    def _ours(self, message: str | None) -> bool:
        return message is not None and message not in self.before

    def translate(self, kind: str, properties: Mapping[str, Any]) -> list[Event]:
        if _session_of(properties) != self.session or self.ended:
            return []
        if kind == "message.updated":
            info = properties.get("info") or {}
            message = str(info.get("id"))
            if self._ours(message):
                role = str(info.get("role"))
                self._roles[message] = role
                self.started = True
                if role == "user" and self.user_message is None:
                    self.user_message = message
                if info.get("error"):
                    self.error = info["error"]
            return []
        if kind == "message.part.delta":
            return self._delta(properties)
        if kind == "message.part.updated":
            return self._part(properties.get("part") or {})
        if kind == "session.status":
            status = (properties.get("status") or {}).get("type")
            if status == "busy":
                self.started = True
            elif status == "idle":
                return self._idle()
            return []
        if kind == "session.idle":
            return self._idle()
        if kind == "session.error":
            if properties.get("error"):
                self.error = properties["error"]
            return []
        return []

    def _assistant(self, message: str | None) -> bool:
        """Whether a message is one of the turn's answers. A part may come before its message:
        once the turn's user message is known, any other new message is an answer."""
        if not self._ours(message):
            return False
        role = self._roles.get(str(message))
        if role is None:
            return self.user_message is not None and message != self.user_message
        return role == "assistant"

    def _delta(self, properties: Mapping[str, Any]) -> list[Event]:
        message = properties.get("messageID")
        part = str(properties.get("partID"))
        if not self._assistant(message) or properties.get("field", "text") != "text":
            return []
        text = str(properties.get("delta", ""))
        kind = self._kinds.get(part)
        if kind not in SHOWN or not text:
            return []
        self._deltas.add(part)
        if kind == "reasoning":
            return [ReasoningDelta(text)]
        self._text.append(text)
        return [TextDelta(text)]

    def _part(self, part: Mapping[str, Any]) -> list[Event]:
        message = part.get("messageID")
        part_id = str(part.get("id"))
        kind = str(part.get("type"))
        self._kinds[part_id] = kind
        if not self._assistant(message):
            return []
        if kind in SHOWN:
            return self._whole(part_id, kind, part)
        if kind == "tool":
            return self._tool(part)
        return []

    def _whole(self, part_id: str, kind: str, part: Mapping[str, Any]) -> list[Event]:
        """A text or reasoning part that ended without deltas: its text all at once."""
        ended = (part.get("time") or {}).get("end") is not None
        text = str(part.get("text") or "")
        if part_id in self._deltas or not ended or not text or part.get("synthetic"):
            return []
        self._deltas.add(part_id)
        if kind == "reasoning":
            return [ReasoningDelta(text)]
        self._text.append(text)
        return [TextDelta(text)]

    def _tool(self, part: Mapping[str, Any]) -> list[Event]:
        call_id = str(part.get("callID") or part.get("id"))
        if call_id in self._answered:
            return []
        state = part.get("state") or {}
        status = state.get("status")
        events: list[Event] = []
        if status in ("running", "completed", "error") and call_id not in self._calls:
            name = self.name(str(part.get("tool", "")))
            self._calls[call_id] = name
            arguments = state.get("input")
            events.append(ToolCall(call_id, name, dict(arguments or {})))
        if status == "completed":
            name = self._calls.pop(call_id)
            self._answered.add(call_id)
            events.append(ToolResult(call_id, name, output_of(state), False))
        elif status == "error":
            name = self._calls.pop(call_id)
            self._answered.add(call_id)
            events.append(ToolResult(call_id, name, str(state.get("error") or ""), True))
        return events

    def _idle(self) -> list[Event]:
        if not self.started:
            return []
        self.ended = True
        # Every call reported has its result before the end.
        events: list[Event] = [
            ToolResult(call_id, name, UNFINISHED, True) for call_id, name in self._calls.items()
        ]
        self._calls.clear()
        return events

    def streamed(self) -> str:
        return "".join(self._text)


def final_message(
    messages: list[Mapping[str, Any]], user_message: str | None, before: frozenset[str]
) -> Mapping[str, Any] | None:
    """The turn's last assistant message, ``{info, parts}``, from the session's messages as
    ``GET /session/:id/message`` lists them, oldest first."""
    found = None
    for message in messages:
        info = message.get("info") or {}
        if info.get("role") != "assistant" or info.get("id") in before:
            continue
        if user_message is None or info.get("parentID") == user_message:
            found = message
    return found


def answer_text(message: Mapping[str, Any]) -> str:
    """The text of an assistant message: its text parts, joined, the synthetic ones left out."""
    return "".join(
        str(part.get("text") or "")
        for part in message.get("parts") or []
        if part.get("type") == "text" and not part.get("synthetic")
    )


def done(message: Mapping[str, Any] | None, streamed: str, checkpoint: str) -> Done:
    text = answer_text(message) if message is not None else ""
    return Done(text or streamed, checkpoint)
