# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""A message and a turn's event as the controller's versioned documents.

The one encoding of ``docs/architecture/data-model.md``, "Documents". A document is the
whole record, a JSON-able mapping the store keeps and never reads inside. Everything is
checked on the way in: a key nobody wrote, an unknown kind, a missing version, a time
without its zone or an id spelt another way is an ``InvalidValueError`` that names what
it found. Every string written is cleaned first: a NUL dropped, an unpaired surrogate
replaced, since PostgreSQL holds neither and a provider or a tool may send either.
"""

from __future__ import annotations

import math
import re
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from robinauts.controller.contract.domain import (
    ArgumentsPiece,
    CallCompleted,
    CallStarted,
    InvalidValueError,
    Message,
    MessageCompleted,
    MessagePart,
    MessageStarted,
    NumberedEvent,
    ReasoningPart,
    ReasoningPiece,
    ResultLanded,
    Role,
    TextPart,
    TextPiece,
    ToolCallPart,
    ToolResultPart,
    TurnEnded,
    TurnEvent,
    TurnStarted,
    TurnState,
)
from robinauts.controller.ports.store import StoredMessage

VERSION = 1
"""One number for both documents. Additive changes keep it; anything else moves it."""

NUL = "\x00"
REPLACEMENT = "\ufffd"
_SURROGATE = re.compile("[\ud800-\udfff]")
_TIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")
_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

MESSAGE_KEYS = frozenset(
    {
        "v",
        "id",
        "session_id",
        "parent_id",
        "role",
        "created_at",
        "agent",
        "engine",
        "model",
        "checkpoint_id",
        "turn_id",
        "parts",
    }
)

MESSAGE_OPTIONAL_KEYS = frozenset({"failed"})
"""Written only when set, so that the messages stored before them read the same."""
PART_KEYS: Mapping[str, frozenset[str]] = {
    "text": frozenset({"kind", "text"}),
    "reasoning": frozenset({"kind", "text"}),
    "tool_call": frozenset({"kind", "call_id", "name", "arguments"}),
    "tool_result": frozenset({"kind", "call_id", "text", "is_error"}),
}
EVENT_HEAD = frozenset({"v", "kind", "turn_id", "position"})
EVENT_KEYS: Mapping[str, frozenset[str]] = {
    "message_started": frozenset({"message_id", "parent_id", "role"}),
    "text_piece": frozenset({"message_id", "text"}),
    "reasoning_piece": frozenset({"message_id", "text"}),
    "call_started": frozenset({"message_id", "call_id", "name"}),
    "arguments_piece": frozenset({"message_id", "call_id", "text"}),
    "call_completed": frozenset({"message_id", "call_id"}),
    "result_landed": frozenset({"message_id", "call_id", "text", "is_error"}),
    "message_completed": frozenset({"message_id"}),
    "turn_ended": frozenset({"state"}),
}


# --- text ---------------------------------------------------------------------


def clean_text(text: str) -> str:
    """``text`` as a store can hold it: no NUL, and no unpaired surrogate.

    A pair that arrived as two code points is joined first, so that only what is still
    half a character is replaced.
    """
    text = text.replace(NUL, "")
    if _SURROGATE.search(text) is None:
        return text
    joined = text.encode("utf-16", "surrogatepass").decode("utf-16", "replace")
    return _SURROGATE.sub(REPLACEMENT, joined)


def clean_json(value: Any, what: str) -> Any:
    """``value`` as JSON a store can hold: every string cleaned, keys included, and no
    number that JSON cannot write."""
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise InvalidValueError(f"{what} holds a number JSON cannot write: {value!r}")
        return value
    if isinstance(value, str):
        return clean_text(value)
    if isinstance(value, list | tuple):
        return [clean_json(item, what) for item in value]
    if isinstance(value, Mapping):
        return {clean_text(str(key)): clean_json(item, what) for key, item in value.items()}
    raise InvalidValueError(f"{what} holds a value that is not JSON: {type(value).__name__}")


# --- spelling ------------------------------------------------------------------


def _write_time(value: datetime, what: str) -> str:
    if value.tzinfo is None:
        raise InvalidValueError(f"{what} is a time without its zone")
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _read_time(value: object, what: str) -> datetime:
    if not isinstance(value, str) or _TIME.match(value) is None:
        raise InvalidValueError(f"{what} is not a time in the document's spelling: {value!r}")
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)


def _read_id(value: object, what: str) -> uuid.UUID:
    if not isinstance(value, str) or _ID.match(value) is None:
        raise InvalidValueError(f"{what} is not an id in the document's spelling: {value!r}")
    return uuid.UUID(value)


def _read_optional_id(value: object, what: str) -> uuid.UUID | None:
    return None if value is None else _read_id(value, what)


def _read_text(value: object, what: str) -> str:
    if not isinstance(value, str):
        raise InvalidValueError(f"{what} is not text: {value!r}")
    return value


def _read_optional_text(value: object, what: str) -> str | None:
    return None if value is None else _read_text(value, what)


def _read_bool(value: object, what: str) -> bool:
    if not isinstance(value, bool):
        raise InvalidValueError(f"{what} is not a boolean: {value!r}")
    return value


def _read_keys(
    document: object, keys: frozenset[str], what: str, optional: frozenset[str] = frozenset()
) -> Mapping[str, Any]:
    if not isinstance(document, Mapping):
        raise InvalidValueError(f"{what} is not a document: {type(document).__name__}")
    unknown = sorted(set(document) - keys - optional)
    if unknown:
        raise InvalidValueError(f"{what} holds a key nobody wrote: {', '.join(unknown)}")
    missing = sorted(keys - set(document))
    if missing:
        raise InvalidValueError(f"{what} lacks a key: {', '.join(missing)}")
    return document


def _read_version(document: Mapping[str, Any], what: str) -> None:
    version = document.get("v")
    if version is None:
        raise InvalidValueError(f"{what} has no version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise InvalidValueError(f"{what} has a version that is not a number: {version!r}")
    if version > VERSION:
        raise InvalidValueError(f"{what} is version {version}, above this build's {VERSION}")


def _read_kind(document: Mapping[str, Any], known: Mapping[str, Any], what: str) -> str:
    kind = document.get("kind")
    if kind not in known:
        raise InvalidValueError(f"{what} is of a kind this build does not know: {kind!r}")
    return kind


# --- messages ------------------------------------------------------------------


def part_to_document(part: MessagePart) -> dict[str, Any]:
    match part:
        case TextPart():
            return {"kind": "text", "text": clean_text(part.text)}
        case ReasoningPart():
            return {"kind": "reasoning", "text": clean_text(part.text)}
        case ToolCallPart():
            return {
                "kind": "tool_call",
                "call_id": clean_text(part.call_id),
                "name": clean_text(part.name),
                "arguments": clean_json(dict(part.arguments), "a tool call's arguments"),
            }
        case ToolResultPart():
            return {
                "kind": "tool_result",
                "call_id": clean_text(part.call_id),
                "text": clean_text(part.text),
                "is_error": bool(part.is_error),
            }
    raise InvalidValueError(f"a part of a kind this build does not know: {type(part).__name__}")


def part_from_document(document: object) -> MessagePart:
    if not isinstance(document, Mapping):
        raise InvalidValueError(f"a part is not a document: {type(document).__name__}")
    kind = _read_kind(document, PART_KEYS, "a part")
    read = _read_keys(document, PART_KEYS[kind], f"a {kind} part")
    match kind:
        case "text":
            return TextPart(_read_text(read["text"], "a text part's text"))
        case "reasoning":
            return ReasoningPart(_read_text(read["text"], "a reasoning part's text"))
        case "tool_call":
            arguments = read["arguments"]
            if not isinstance(arguments, Mapping):
                raise InvalidValueError("a tool call's arguments are not an object")
            return ToolCallPart(
                _read_text(read["call_id"], "a tool call's id"),
                _read_text(read["name"], "a tool call's name"),
                dict(arguments),
            )
    return ToolResultPart(
        _read_text(read["call_id"], "a tool result's call id"),
        _read_text(read["text"], "a tool result's text"),
        _read_bool(read["is_error"], "a tool result's is_error"),
    )


def message_to_document(message: Message) -> dict[str, Any]:
    """The whole message, ``v`` first."""
    return {
        "v": VERSION,
        "id": str(message.id),
        "session_id": str(message.session_id),
        "parent_id": None if message.parent_id is None else str(message.parent_id),
        "role": message.role.value,
        "created_at": _write_time(message.created_at, "a message's created_at"),
        "agent": None if message.agent is None else clean_text(message.agent),
        "engine": None if message.engine is None else clean_text(message.engine),
        "model": None if message.model is None else clean_text(message.model),
        "checkpoint_id": (
            None if message.checkpoint_id is None else clean_text(message.checkpoint_id)
        ),
        "turn_id": None if message.turn_id is None else str(message.turn_id),
        "parts": [part_to_document(part) for part in message.parts],
    } | ({"failed": True} if message.failed else {})


def message_from_document(document: object) -> Message:
    if not isinstance(document, Mapping):
        raise InvalidValueError(f"a message is not a document: {type(document).__name__}")
    _read_version(document, "a message")
    read = _read_keys(document, MESSAGE_KEYS, "a message", MESSAGE_OPTIONAL_KEYS)
    role = read["role"]
    if role not in {r.value for r in Role}:
        raise InvalidValueError(f"a message's role is not a role: {role!r}")
    parts = read["parts"]
    if not isinstance(parts, list):
        raise InvalidValueError("a message's parts are not a list")
    return Message(
        _read_id(read["id"], "a message's id"),
        _read_id(read["session_id"], "a message's session_id"),
        parent_id=_read_optional_id(read["parent_id"], "a message's parent_id"),
        role=Role(role),
        parts=tuple(part_from_document(part) for part in parts),
        created_at=_read_time(read["created_at"], "a message's created_at"),
        agent=_read_optional_text(read["agent"], "a message's agent"),
        engine=_read_optional_text(read["engine"], "a message's engine"),
        model=_read_optional_text(read["model"], "a message's model"),
        checkpoint_id=_read_optional_text(read["checkpoint_id"], "a message's checkpoint_id"),
        turn_id=_read_optional_id(read["turn_id"], "a message's turn_id"),
        failed=_read_failed(read.get("failed", False)),
    )


def _read_failed(value: object) -> bool:
    if value is not True and value is not False:
        raise InvalidValueError(f"a message's failed is not true or false: {value!r}")
    return value


# --- events --------------------------------------------------------------------


def event_to_document(turn_id: uuid.UUID, position: int, event: TurnEvent) -> dict[str, Any]:
    """One event at its position. ``TurnStarted`` has no document: it is never stored."""
    head: dict[str, Any] = {"v": VERSION, "kind": "", "turn_id": str(turn_id), "position": position}
    match event:
        case MessageStarted():
            body: dict[str, Any] = {
                "message_id": str(event.message_id),
                "parent_id": str(event.parent_id),
                "role": event.role.value,
            }
            kind = "message_started"
        case TextPiece():
            body, kind = (
                {
                    "message_id": str(event.message_id),
                    "text": clean_text(event.text),
                },
                "text_piece",
            )
        case ReasoningPiece():
            body = {"message_id": str(event.message_id), "text": clean_text(event.text)}
            kind = "reasoning_piece"
        case CallStarted():
            body = {
                "message_id": str(event.message_id),
                "call_id": clean_text(event.call_id),
                "name": clean_text(event.name),
            }
            kind = "call_started"
        case ArgumentsPiece():
            body = {
                "message_id": str(event.message_id),
                "call_id": clean_text(event.call_id),
                "text": clean_text(event.text),
            }
            kind = "arguments_piece"
        case CallCompleted():
            body = {"message_id": str(event.message_id), "call_id": clean_text(event.call_id)}
            kind = "call_completed"
        case ResultLanded():
            body = {
                "message_id": str(event.message_id),
                "call_id": clean_text(event.call_id),
                "text": clean_text(event.text),
                "is_error": bool(event.is_error),
            }
            kind = "result_landed"
        case MessageCompleted():
            body, kind = {"message_id": str(event.message_id)}, "message_completed"
        case TurnEnded():
            body, kind = {"state": event.state.value}, "turn_ended"
        case TurnStarted():
            raise InvalidValueError("TurnStarted has no document")
        case _:
            raise InvalidValueError(
                f"an event of a kind this build does not know: {type(event).__name__}"
            )
    head["kind"] = kind
    return head | body


def event_from_document(document: object) -> NumberedEvent:
    if not isinstance(document, Mapping):
        raise InvalidValueError(f"an event is not a document: {type(document).__name__}")
    _read_version(document, "an event")
    kind = _read_kind(document, EVENT_KEYS, "an event")
    read = _read_keys(document, EVENT_HEAD | EVENT_KEYS[kind], f"a {kind} event")
    _read_id(read["turn_id"], "an event's turn_id")
    position = read["position"]
    if not isinstance(position, int) or isinstance(position, bool) or position < 1:
        raise InvalidValueError(f"an event's position is not a position: {position!r}")
    event: TurnEvent
    match kind:
        case "message_started":
            role = read["role"]
            if role not in {r.value for r in Role}:
                raise InvalidValueError(f"a message_started event's role is not a role: {role!r}")
            event = MessageStarted(
                _read_id(read["message_id"], "an event's message_id"),
                _read_id(read["parent_id"], "an event's parent_id"),
                Role(role),
            )
        case "text_piece":
            event = TextPiece(
                _read_id(read["message_id"], "an event's message_id"),
                _read_text(read["text"], "a text_piece event's text"),
            )
        case "reasoning_piece":
            event = ReasoningPiece(
                _read_id(read["message_id"], "an event's message_id"),
                _read_text(read["text"], "a reasoning_piece event's text"),
            )
        case "call_started":
            event = CallStarted(
                _read_id(read["message_id"], "an event's message_id"),
                _read_text(read["call_id"], "an event's call_id"),
                _read_text(read["name"], "a call_started event's name"),
            )
        case "arguments_piece":
            event = ArgumentsPiece(
                _read_id(read["message_id"], "an event's message_id"),
                _read_text(read["call_id"], "an event's call_id"),
                _read_text(read["text"], "an arguments_piece event's text"),
            )
        case "call_completed":
            event = CallCompleted(
                _read_id(read["message_id"], "an event's message_id"),
                _read_text(read["call_id"], "an event's call_id"),
            )
        case "result_landed":
            event = ResultLanded(
                _read_id(read["message_id"], "an event's message_id"),
                _read_text(read["call_id"], "an event's call_id"),
                _read_text(read["text"], "a result_landed event's text"),
                _read_bool(read["is_error"], "a result_landed event's is_error"),
            )
        case "message_completed":
            event = MessageCompleted(_read_id(read["message_id"], "an event's message_id"))
        case _:
            state = read["state"]
            if state not in {s.value for s in TurnState}:
                raise InvalidValueError(f"a turn_ended event's state is not a state: {state!r}")
            event = TurnEnded(TurnState(state))
    return NumberedEvent(position, event)


def stored_message(message: Message) -> StoredMessage:
    """A message as the store keeps it: the columns it orders and joins by, and the document."""
    return StoredMessage(
        message.id,
        message.session_id,
        message.parent_id,
        message.role,
        message.created_at,
        message_to_document(message),
    )
