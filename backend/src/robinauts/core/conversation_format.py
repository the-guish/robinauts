# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The one canonical encoding of the conversation format, owned by core.

A message is stored, exported and sent over the wire as plain JSON-able data:
dictionaries, lists, strings, integers, ``None``. Both directions of that
encoding are written once, here -- ``domain`` holds the records and checks
them, ``core`` turns them into data and back -- so that the datastore, an
export and the api cannot drift into three spellings of the same conversation,
and so that what comes back out of a row is checked exactly as hard as what an
engine produced.

**One instant has one spelling.** A time is always written in UTC, with its
offset, and with microseconds only when it has any; a time is read from any
offset, ``Z`` included. So two records of the same moment encode identically
and a row can be compared with another.

**Two documents carry a version**, and they are upgraded apart. A *message
document* is a whole message (``message_to_data``): what crosses the
conversation port, what a store keeps, what an export writes. A *run event
document* is one event of a run with its position (``run_event_to_data``):
what the events table keeps and what the wire carries. One number is used for
both -- the format has one version -- but each shape has its own table of
upgrades, because an upgrade written for one handed the other would make
nonsense of it. A message's parts are written **inside** its document and
have no document of their own: one shape at the port is one thing to version,
to upgrade and to get wrong.

**Versioned, and readable forward.** Everything written carries
``format_version``, and it is written and read **by us**: a request never
carries one. The api takes a message from a browser as the fields of that
request and builds the record itself, so ``UnsupportedFormatError`` is
something only our own rows can cause. The rules
(``docs/specs/conversations.md``):

- there are three **additive** changes, and none of them moves the version: a
  new kind of content, a new role, and anything at all under the reserved
  ``extras`` key, which a build that does not use it reads past. A build that
  does not carry a kind refuses **the message holding it**, by name, and
  reads every other message;
- any **other** new key moves it, because nothing but ``extras`` is read
  past: a build that dropped a field it did not recognise would write the
  message back without it;
- so does any change to the **meaning or the shape** of what is already
  written;
- a build reads every version up to its own, lifting older data one version
  at a time through the table for that document's shape, and refuses a
  version **above** its own, because it cannot know what that one means;
- writing always writes the current version.

**Loud about what it does not know.** A message holding content of a kind the
format names and this build does not carry (an image) is refused
by name with ``UnsupportedContentError`` -- whatever else that piece of
content carries, because the kind is read before anything else. The message is
refused whole: one shown without its image is one misread. A key nobody wrote, a naive
time, an id spelt some other way: ``InvalidValueError``. Nothing is ignored
and nothing is guessed at, because both would lose part of a conversation
silently.

**This is for data the platform itself wrote.** Everything here requires a
``format_version``, and a request never carries one: a browser sends the
fields of a message and the application builds the record from them. What
comes through here is an export being read back, and rows.

**The callers of the ``_stored`` readers are the application**, never a
store. A store is handed the document this writes, keeps it as it is, and
hands it back; it may not import ``core`` at all (``docs/layout.md``), which
is the same rule as for the configuration file -- an adapter reads, ``core``
validates. What a store does build out of its own columns are the flat
records it owns those columns for, inside
``robinauts.domain.reading_stored``.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from copy import deepcopy
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from robinauts.domain import (
    FORMAT_VERSION,
    MAX_EXTRAS_BYTES,
    MAX_EXTRAS_DEPTH,
    MAX_EXTRAS_NODES,
    MAX_PARTS,
    Channel,
    Engine,
    InvalidValueError,
    Message,
    MessageCompleted,
    MessagePart,
    MessageStarted,
    PartKind,
    Provenance,
    ReasoningDelta,
    ReasoningPart,
    Role,
    RunEnded,
    RunEvent,
    RunStarted,
    RunState,
    TextDelta,
    TextPart,
    ToolCallPart,
    ToolResultPart,
    TurnEvent,
    UnsupportedFormatError,
    check_supported,
    check_supported_role,
    checked_data,
    checked_instant,
    checked_parts,
    describe,
    reading_stored,
)

EXTRAS = "extras"
"""The one key a build may meet, not understand, and read past.

Everything else unknown is refused, because a build that dropped a field it
did not recognise would write the message back without it. This key is the
exception, and it is an exception with a shape: an object, bounded, whose
contents are nobody else's business. A build that does not use what is in
there reads past it, and one that does knows what it put there. **This build
carries it on a message** -- ``Message.extras``, where the vendor's signed
thinking blocks live, keyed by vendor, stored unread and written back as they
were (``docs/specs/conversations.md``, "Reasoning") -- and reads past it on a
part and on an event, which it never writes. The bounds are
``domain.MAX_EXTRAS_BYTES``, ``MAX_EXTRAS_DEPTH`` and ``MAX_EXTRAS_NODES``,
which a tool call's arguments share.
"""

__all__ = ["MAX_EXTRAS_BYTES", "MAX_EXTRAS_DEPTH", "MAX_EXTRAS_NODES"]

_PART_TYPES: Mapping[PartKind, type[MessagePart]] = {
    PartKind.TEXT: TextPart,
    PartKind.REASONING: ReasoningPart,
    PartKind.TOOL_CALL: ToolCallPart,
    PartKind.TOOL_RESULT: ToolResultPart,
}
"""The record each kind this build carries is read into."""

_PART_KEYS: Mapping[PartKind, frozenset[str]] = {
    PartKind.TEXT: frozenset({"kind", "text", EXTRAS}),
    PartKind.REASONING: frozenset({"kind", "text", EXTRAS}),
    PartKind.TOOL_CALL: frozenset({"kind", "call_id", "name", "arguments", EXTRAS}),
    PartKind.TOOL_RESULT: frozenset({"kind", "call_id", "text", "is_error", EXTRAS}),
}
"""What each kind is written with. A kind has its own keys; they are checked
against the kind, once the kind is known."""

_PROVENANCE_KEYS = frozenset({"agent", "engine", "model", "run_id"})
_MESSAGE_KEYS = frozenset(
    {
        "format_version",
        "id",
        "conversation_id",
        "parent_id",
        "role",
        "parts",
        "created_at",
        "channel",
        "provenance",
        EXTRAS,
    }
)

type Upgrade = Callable[[dict[str, Any]], dict[str, Any]]

_MESSAGE_UPGRADES: Mapping[int, Upgrade] = {}
"""How a **message document** of one version is read into the next.

A message document is what ``message_to_data`` writes: the whole message,
``format_version`` and all. An upgrader here is handed a copy of one, of the
version it is registered under, and returns one of the next version --
``format_version`` included, since that is what says it worked.

Empty while version 1 is the first and only one. A registry per document
shape, because the two shapes are not the same document: an upgrade written
for a message would be handed a content column and make nonsense of it.
"""

_EVENT_UPGRADES: Mapping[int, Upgrade] = {}
"""How a **run event document** of one version is read into the next.

A run event document is what ``run_event_to_data`` writes: ``format_version``,
the run, the position and the event. Same contract as above, over that shape.
"""


# --- encoding --------------------------------------------------------------


def part_to_data(part: MessagePart) -> dict[str, Any]:
    """One part as plain data, ``kind`` in front.

    Dispatched on the record, kind by kind, with nowhere for a kind nobody
    wrote an encoding for to fall through to: a record added to
    ``MessagePart`` and forgotten here stops, rather than being written out as
    whatever fields it happens to share with the ones above it.
    """
    if isinstance(part, TextPart | ReasoningPart):
        return {"kind": part.kind.value, "text": part.text}
    if isinstance(part, ToolCallPart):
        return {
            "kind": part.kind.value,
            "call_id": part.call_id,
            "name": part.name,
            "arguments": deepcopy(dict(part.arguments)),
        }
    if isinstance(part, ToolResultPart):
        return {
            "kind": part.kind.value,
            "call_id": part.call_id,
            "text": part.text,
            "is_error": part.is_error,
        }
    raise InvalidValueError(f"there is no encoding for {describe(part)}")


def message_to_data(message: Message) -> dict[str, Any]:
    """A whole message as plain data: what an export writes and a store keeps.

    ``extras`` is written when there is something in it and left out when
    there is not: it is the one key a document may be written without, and a
    message with nothing of a vendor's is written as it always was.
    """
    if not isinstance(message, Message):
        raise InvalidValueError(f"a message is a Message, not {describe(message)}")
    data = {
        "format_version": FORMAT_VERSION,
        "id": str(message.id),
        "conversation_id": str(message.conversation_id),
        "parent_id": None if message.parent_id is None else str(message.parent_id),
        "role": message.role.value,
        "parts": [part_to_data(part) for part in message.parts],
        "created_at": instant(message.created_at),
        "channel": message.channel.value,
        "provenance": (
            None if message.provenance is None else provenance_to_data(message.provenance)
        ),
    }
    if message.extras:
        data[EXTRAS] = deepcopy(dict(message.extras))
    return data


def provenance_to_data(provenance: Provenance) -> dict[str, Any]:
    """What produced an answer, as plain data."""
    if not isinstance(provenance, Provenance):
        raise InvalidValueError(f"provenance is a Provenance, not {describe(provenance)}")
    return {
        "agent": provenance.agent,
        "engine": provenance.engine.value,
        "model": provenance.model,
        "run_id": str(provenance.run_id),
    }


def instant(when: datetime) -> str:
    """``when`` in the one spelling the format writes: UTC, with its offset."""
    return _within(when, "a time").isoformat()


def _within(when: datetime, what: str) -> datetime:
    """``when`` as UTC, which every record's own rule has already allowed.

    ``robinauts.domain.checked_instant`` is where the window is enforced, on
    every record that holds a time. This is the same check at the moment of
    writing, because what is encoded here is not always something that came
    out of a record.
    """
    return checked_instant(when, what).astimezone(UTC)


# --- decoding --------------------------------------------------------------


def part_from_data(data: object) -> MessagePart:
    """One part, read back from plain data.

    The kind is resolved **first**: a kind this build does not carry is
    refused as unsupported however it is written, rather than reported as
    malformed because of keys that belong to a kind we have never built.
    """
    fields = _mapping(data, "a part")
    kind = check_supported(_enum(fields.get("kind"), PartKind, "a part's kind"))
    _keys(fields, _PART_KEYS[kind], f"a {kind.value} part")
    _check_extras(fields.get(EXTRAS), "a part's extras")
    if kind is PartKind.TOOL_CALL:
        return ToolCallPart(
            call_id=_required(fields, "call_id", "a tool call's id"),
            name=_required(fields, "name", "a tool call's name"),
            arguments=checked_data(fields.get("arguments"), "a tool call's arguments"),
        )
    if kind is PartKind.TOOL_RESULT:
        is_error = fields.get("is_error")
        if not isinstance(is_error, bool):
            raise InvalidValueError(
                f"whether a tool result is an error is yes or no, not {describe(is_error)}"
            )
        return ToolResultPart(
            call_id=_required(fields, "call_id", "a tool result's call id"),
            text=_required(fields, "text", "a part's text"),
            is_error=is_error,
        )
    return _PART_TYPES[kind](text=_required(fields, "text", "a part's text"))


def message_from_data(data: object) -> Message:
    """A whole message, read back from plain data. Every field is checked."""
    fields = _at_current_version(_mapping(data, "a message"), "a message", _MESSAGE_UPGRADES)
    _keys(fields, _MESSAGE_KEYS, "a message")
    extras = _check_extras(fields.get(EXTRAS), "a message's extras")
    role = check_supported_role(_enum(fields.get("role"), Role, "a message's role"))
    return Message(
        id=_uuid(fields.get("id"), "a message's id"),
        conversation_id=_uuid(fields.get("conversation_id"), "a message's conversation id"),
        parent_id=_optional_uuid(fields.get("parent_id"), "a message's parent id"),
        role=role,
        parts=_parts(fields.get("parts")),
        created_at=_when(fields.get("created_at"), "created_at"),
        channel=_enum(fields.get("channel"), Channel, "a message's channel"),
        provenance=_provenance(fields.get("provenance")),
        extras=extras,
    )


# --- reading our own rows ---------------------------------------------------


def message_from_stored(data: object) -> Message:
    """A message read out of our own store; any refusal is ``StoredDataError``."""
    with reading_stored("a stored message cannot be read by this build"):
        return message_from_data(data)


# --- the pieces -------------------------------------------------------------


def _provenance(data: object) -> Provenance | None:
    if data is None:
        return None
    fields = _mapping(data, "a message's provenance")
    _keys(fields, _PROVENANCE_KEYS, "a message's provenance")
    return Provenance(
        agent=_required(fields, "agent", "an agent's id"),
        engine=_enum(fields.get("engine"), Engine, "an engine"),
        model=_required(fields, "model", "a model's id"),
        run_id=_uuid(fields.get("run_id"), "a run's id"),
    )


def _parts(data: object) -> tuple[MessagePart, ...]:
    """The parts of a message, bounded and non-empty as a message's are.

    ``checked_parts`` is the record's own rule, so a row holding no content at
    all, or a thousand parts, is refused where it is read and not only where
    one is built in code.
    """
    if isinstance(data, str) or not isinstance(data, list | tuple):
        raise InvalidValueError(f"a message's parts are a list, not {describe(data)}")
    if len(data) > MAX_PARTS:
        raise InvalidValueError(f"a message has at most {MAX_PARTS} parts, not {len(data)}")
    return checked_parts([part_from_data(part) for part in data])


def _at_current_version(
    fields: Mapping[str, Any], what: str, upgrades: Mapping[int, Upgrade]
) -> Mapping[str, Any]:
    """``fields`` lifted to this build's version, or a refusal saying why not.

    ``upgrades`` is the table for **this** document's shape.
    """
    version = _version(fields, what)
    if version > FORMAT_VERSION:
        raise UnsupportedFormatError(
            f"{what} is written in conversation format version {version};"
            f" this build reads up to {FORMAT_VERSION}"
        )
    while version < FORMAT_VERSION:
        upgrade = upgrades.get(version)
        if upgrade is None:
            raise UnsupportedFormatError(
                f"{what} is written in conversation format version {version},"
                " which this build has no upgrade from"
            )
        fields = _mapping(upgrade(deepcopy(dict(fields))), what)
        version, before = _version(fields, what), version
        if version <= before:
            raise UnsupportedFormatError(
                f"the upgrade from conversation format version {before} did not move it forward"
            )
        if version > FORMAT_VERSION:
            raise UnsupportedFormatError(
                f"the upgrade from conversation format version {before} went past"
                f" version {FORMAT_VERSION}, which is as far as this build reads"
            )
    return fields


def _check_extras(data: object, what: str) -> dict[str, Any]:
    """``extras``, once it is the shape the format reserves for it; empty if absent.

    The shape and the bounds are ``domain.checked_data``'s, which a tool
    call's arguments share. What is read is returned so that a message keeps
    its own; a part's and an event's are checked and dropped, since this
    build writes neither. What this must not do is accept something of
    another shape, or of no bounded size, and hand a store a row it will
    choke on.
    """
    if data is None:
        return {}
    return checked_data(data, what)


def _version(fields: Mapping[str, Any], what: str) -> int:
    """The version ``fields`` was written in.

    ``True`` is not a version: ``bool`` is an ``int`` in Python, and JSON that
    said ``true`` would otherwise pass for version 1.
    """
    version = fields.get("format_version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 0:
        raise UnsupportedFormatError(
            f"{what} does not say which version of the conversation format it is written in"
        )
    return version


def _mapping(data: object, what: str) -> Mapping[str, Any]:
    if not isinstance(data, Mapping):
        raise InvalidValueError(f"{what} is an object, not {describe(data)}")
    if not all(isinstance(key, str) for key in data):
        raise InvalidValueError(f"the keys of {what} are names")
    return data


def _keys(fields: Mapping[str, Any], allowed: frozenset[str], what: str) -> None:
    """Every key this build holds, and none it does not.

    **Present**, not merely allowed: a document is written whole, so a key
    that is not there is a document somebody else wrote, and reading a
    missing ``parent_id`` as "this is a root" would quietly reparent a
    message. ``extras`` is the one that may be left out, since it is the one
    a build is allowed not to know about.

    What is unknown is counted, not named: a key is as much the sender's text
    as a value is, and this message is copied into logs and crash reports.
    The missing ones are named, because those names are ours.
    """
    unknown = len(set(fields) - allowed)
    if unknown:
        raise InvalidValueError(
            f"{what} has {unknown} key(s) that are no part of it;"
            f" it holds {', '.join(sorted(allowed))}"
        )
    missing = sorted(allowed - {EXTRAS} - set(fields))
    if missing:
        raise InvalidValueError(f"{what} is written with {', '.join(missing)}")


def _required(fields: Mapping[str, Any], key: str, what: str) -> str:
    value = fields.get(key)
    if not isinstance(value, str):
        raise InvalidValueError(f"{what} is text, not {describe(value)}")
    return value


def _enum[EnumT: StrEnum](value: object, kind: type[EnumT], what: str) -> EnumT:
    """The member of ``kind`` spelt ``value``; its own error if there is none.

    A kind the format names and this build does not carry is somebody else's
    refusal (``check_supported``): this one is for a spelling that is in no
    version of the format at all.
    """
    if not isinstance(value, str):
        raise InvalidValueError(f"{what} is text, not {describe(value)}")
    try:
        return kind(value)
    except ValueError:
        raise InvalidValueError(f"{what} is none this format has a name for") from None


def _uuid(value: object, what: str) -> uuid.UUID:
    """An id from its one spelling: 36 lower-case hexadecimal characters.

    The forms ``uuid.UUID`` also accepts -- braces, a URN, no hyphens, upper
    case -- are refused, so that one id has one encoding and a store cannot
    hold two rows that mean the same message.
    """
    if not isinstance(value, str):
        raise InvalidValueError(f"{what} is text, not {describe(value)}")
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        raise InvalidValueError(f"{what} is a UUID, not text of {len(value)} characters") from None
    if str(parsed) != value:
        raise InvalidValueError(f"{what} is written as 36 lower-case characters with its hyphens")
    return parsed


def _optional_uuid(value: object, what: str) -> uuid.UUID | None:
    return None if value is None else _uuid(value, what)


def _when(value: object, what: str) -> datetime:
    """A time from ISO 8601 text, with its offset. Naive text is refused."""
    if not isinstance(value, str):
        raise InvalidValueError(f"{what} is text, not {describe(value)}")
    try:
        when = datetime.fromisoformat(value)
    except ValueError:
        raise InvalidValueError(f"{what} is a time in ISO 8601") from None
    if when.tzinfo is None or when.utcoffset() is None:
        raise InvalidValueError(f"{what} must carry its time zone")
    return _within(when, what)


# --- the events of a run ----------------------------------------------------

RUN_STARTED = "run_started"
MESSAGE_STARTED = "message_started"
TEXT_DELTA = "text_delta"
REASONING_DELTA = "reasoning_delta"
MESSAGE_COMPLETED = "message_completed"
RUN_ENDED = "run_ended"

_EVENT_KIND: Mapping[type, str] = {
    RunStarted: RUN_STARTED,
    MessageStarted: MESSAGE_STARTED,
    TextDelta: TEXT_DELTA,
    ReasoningDelta: REASONING_DELTA,
    MessageCompleted: MESSAGE_COMPLETED,
    RunEnded: RUN_ENDED,
}
"""The name each event is written under. These are stored data: they do not
change, and a kind added later is added here.
"""

_EVENT_KEYS: Mapping[str, frozenset[str]] = {
    RUN_STARTED: frozenset({"kind", "run_id", "conversation_id", EXTRAS}),
    MESSAGE_STARTED: frozenset({"kind", "run_id", "message_id", "parent_id", "role", EXTRAS}),
    TEXT_DELTA: frozenset({"kind", "run_id", "message_id", "text", EXTRAS}),
    REASONING_DELTA: frozenset({"kind", "run_id", "message_id", "text", EXTRAS}),
    MESSAGE_COMPLETED: frozenset({"kind", "run_id", "message", EXTRAS}),
    RUN_ENDED: frozenset({"kind", "run_id", "state", "error", EXTRAS}),
}
"""What each kind of event is written with; checked against the kind, once
the kind is known, exactly as a part's keys are."""

_RUN_EVENT_KEYS = frozenset({"format_version", "run_id", "seq", "event", EXTRAS})


def event_to_data(event: TurnEvent) -> dict[str, Any]:
    """One turn event as plain data, ``kind`` in front.

    **A piece of a run event document**, which is where the version is, and
    never a document of its own: a bare event is not stored and is not read
    back from a store. It is the building block of the envelope below, and of
    whatever the wire wraps an event in.
    """
    if isinstance(event, RunStarted):
        return {
            "kind": RUN_STARTED,
            "run_id": str(event.run_id),
            "conversation_id": str(event.conversation_id),
        }
    if isinstance(event, MessageStarted):
        return {
            "kind": MESSAGE_STARTED,
            "run_id": str(event.run_id),
            "message_id": str(event.message_id),
            "parent_id": str(event.parent_id),
            "role": event.role.value,
        }
    if isinstance(event, TextDelta | ReasoningDelta):
        return {
            "kind": _EVENT_KIND[type(event)],
            "run_id": str(event.run_id),
            "message_id": str(event.message_id),
            "text": event.text,
        }
    if isinstance(event, MessageCompleted):
        return {
            "kind": MESSAGE_COMPLETED,
            "run_id": str(event.run_id),
            "message": message_to_data(event.message),
        }
    if isinstance(event, RunEnded):
        return {
            "kind": RUN_ENDED,
            "run_id": str(event.run_id),
            "state": event.state.value,
            "error": event.error,
        }
    raise InvalidValueError(f"there is no encoding for {describe(event)}")


def run_event_to_data(event: RunEvent) -> dict[str, Any]:
    """One event of a run, with its position: what the events table keeps."""
    if not isinstance(event, RunEvent):
        raise InvalidValueError(f"a run event is a RunEvent, not {describe(event)}")
    return {
        "format_version": FORMAT_VERSION,
        "run_id": str(event.run_id),
        "seq": event.seq,
        "event": event_to_data(event.event),
    }


def event_from_data(data: object) -> TurnEvent:
    """One turn event, read back from plain data.

    The kind is resolved first and the keys are checked against **that**
    kind, as a part's are: what a kind this build does not know carries is
    not a reason to call it malformed.
    """
    fields = _mapping(data, "a turn event")
    kind = _required(fields, "kind", "a turn event's kind")
    if kind not in _EVENT_KEYS:
        raise InvalidValueError("a turn event of a kind this format has no name for")
    _keys(fields, _EVENT_KEYS[kind], f"a {kind} event")
    _check_extras(fields.get(EXTRAS), "a turn event's extras")
    run_id = _uuid(fields.get("run_id"), "a run's id")
    if kind == RUN_STARTED:
        return RunStarted(
            run_id=run_id,
            conversation_id=_uuid(fields.get("conversation_id"), "a conversation's id"),
        )
    if kind == MESSAGE_STARTED:
        return MessageStarted(
            run_id=run_id,
            message_id=_uuid(fields.get("message_id"), "a message's id"),
            parent_id=_uuid(fields.get("parent_id"), "a message's parent id"),
            role=check_supported_role(_enum(fields.get("role"), Role, "a message's role")),
        )
    if kind in (TEXT_DELTA, REASONING_DELTA):
        made = TextDelta if kind == TEXT_DELTA else ReasoningDelta
        return made(
            run_id=run_id,
            message_id=_uuid(fields.get("message_id"), "a message's id"),
            text=_required(fields, "text", "a delta's text"),
        )
    if kind == MESSAGE_COMPLETED:
        return MessageCompleted(run_id=run_id, message=message_from_data(fields.get("message")))
    error = fields.get("error")
    if error is not None and not isinstance(error, str):
        raise InvalidValueError(f"a run's error is text, not {describe(error)}")
    return RunEnded(
        run_id=run_id,
        state=_enum(fields.get("state"), RunState, "a run's state"),
        error=error,
    )


def run_event_from_data(data: object) -> RunEvent:
    """One event of a run, with its position, read back from plain data."""
    fields = _at_current_version(_mapping(data, "a run event"), "a run event", _EVENT_UPGRADES)
    _keys(fields, _RUN_EVENT_KEYS, "a run event")
    _check_extras(fields.get(EXTRAS), "a run event's extras")
    seq = fields.get("seq")
    if isinstance(seq, bool) or not isinstance(seq, int):
        raise InvalidValueError(f"an event's position is a whole number, not {describe(seq)}")
    return RunEvent(
        run_id=_uuid(fields.get("run_id"), "a run's id"),
        seq=seq,
        event=event_from_data(fields.get("event")),
    )


def run_event_from_stored(data: object) -> RunEvent:
    """An event of a run, with its position, read the same way."""
    with reading_stored("a stored run event cannot be read by this build"):
        return run_event_from_data(data)
