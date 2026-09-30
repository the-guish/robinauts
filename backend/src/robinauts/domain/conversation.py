# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The platform's own conversation format: the tree, and what is in a message.

Owned by the platform, not by an agent framework and not by a model vendor
(`ADR 0002 <../../../docs/adr/0002-conversation-persistence.md>`_). Four seams
meet on these records -- both agent engines, the datastore, the wire and
export -- so what is here is deliberately small and explicit: nothing in it
is shaped by LangChain, Pydantic AI, OpenAI or Anthropic, and every name is
one the specs use.

A conversation is **stored** as a tree: every message has a parent, editing
a question or regenerating an answer writes a new message beside the old
one, and nothing is overwritten (``docs/specs/conversations.md``). What a
reader sees is one path of it. The rules over a collection of messages --
which parents are legal, which path is the visible one -- are pure functions
in ``robinauts.core.conversation_tree``, and the one
encoding of all this is ``robinauts.core.conversation_format``. A record here
holds and checks; it does not decide and it does not serialise.

**Room without building it.** The format names every kind of content the
specs give a message -- text, image, file, reasoning, tool call, tool result
-- and this version carries four of them: text, reasoning, and the two tool
parts. The other kinds are refused, by name, as not supported yet. Naming
them now is what lets them arrive without a stored conversation having to be
rewritten: the discriminator they will be stored under is already reserved,
and a build that meets one it cannot carry says so instead of guessing.

**Tools, in the format** (``docs/specs/conversations.md``, "A turn is a
chain"). A call the model makes is a ``ToolCallPart`` of the **assistant**
message that made it: the call's id, the tool's full name, its arguments as
data. The results of one call batch are one message of the ``tool`` role
under that assistant message, holding one ``ToolResultPart`` per call, each
naming the call it answers; that a tool message answers exactly its parent's
calls is a rule of the tree (``robinauts.core.conversation_tree``), since it
needs the parent. What a message of each role may hold is a rule of the
record, here: a tool message holds results and nothing else, a user message
holds no tool part, and an assistant message holds no result.

**``extras``** is the one key of a document a build is allowed not to read
(``docs/specs/conversations.md``, "The version"): an object, bounded, keyed
by vendor, holding what a vendor needs back with the history and the platform
never reads -- the signed thinking blocks an answer that makes a tool call
carries (``extras.anthropic``). It is carried on every message, empty for
nearly all of them, stored unread and written back as it was.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, ClassVar

from robinauts.domain.agents import Engine, checked_config_id
from robinauts.domain.errors import InvalidValueError, UnsupportedContentError
from robinauts.domain.tools import checked_call_id, checked_tool_name
from robinauts.domain.values import (
    MAX_PART_CHARS,
    checked_data,
    checked_instant,
    checked_line,
    checked_text,
    checked_uuid,
    describe,
)

FORMAT_VERSION = 1
"""The version of this format, recorded on everything written in it.

One number for the format, and it moves rarely. There are three ways to add
without moving it: a new kind of content, a new role, and anything at all
under the reserved ``extras`` key, which a build that does not use it reads
past. **Any other new key moves it**, as does any change to the meaning or
the shape of what is already written. A build reads every version up to its
own and refuses one above it. The rules and the upgrade path are in
``robinauts.core.conversation_format``.
"""

MAX_PARTS = 64
"""How many parts one message may hold."""

MAX_MESSAGE_CHARS = MAX_PARTS * MAX_PART_CHARS
"""The longest one message's text may be: every part of it, full.

Derived rather than chosen, so that it cannot drift from the two bounds it is
made of. It is what ``text_parts`` refuses past, and it is the bound the wire
states for a message somebody writes (``robinauts.api.schemas``): a client
that knows it can say so in its own form instead of finding out by being
refused.
"""

MAX_TITLE_CHARS = 120
"""The longest a conversation's title may be; it is shown in a list."""


class Role(StrEnum):
    """Who a message is from.

    ``TOOL`` is the platform's own: a message of that role holds the results
    of one batch of the calls its parent made, and is written by the
    application and never by a person or a model
    (``docs/specs/conversations.md``). What may follow what is
    ``robinauts.core.conversation_tree.may_follow``.
    """

    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


SUPPORTED_ROLES: frozenset[Role] = frozenset(Role)
"""The roles this version carries: every one the format names.

Kept as a set, and checked where a role is read, so that a role added to the
format later is refused by name by a build that does not carry it, exactly
as a kind of content is.
"""


class Channel(StrEnum):
    """Where a message came from (``docs/specs/channels.md``).

    A conversation is not tied to one: each message records its own, so a
    conversation begun in the web UI and continued from somewhere else says
    which was which. Other channels join this enum with the bridge that
    serves them; a stored ``web`` never changes meaning.
    """

    WEB = "web"


class PartKind(StrEnum):
    """Every kind of content a message may hold (``docs/specs/conversations.md``).

    The whole table is named here, including the kinds this version does not
    carry, because these values are the discriminator stored data is written
    under. ``SUPPORTED_PART_KINDS`` says which of them exist as records today.
    """

    TEXT = "text"
    IMAGE = "image"
    FILE = "file"
    REASONING = "reasoning"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"


SUPPORTED_PART_KINDS: frozenset[PartKind] = frozenset(
    {PartKind.TEXT, PartKind.REASONING, PartKind.TOOL_CALL, PartKind.TOOL_RESULT}
)
"""The kinds this version has a record for. The others are refused by name."""


def check_supported(kind: PartKind) -> PartKind:
    """``kind`` if this build carries it; ``UnsupportedContentError`` if not."""
    if kind not in SUPPORTED_PART_KINDS:
        raise UnsupportedContentError(f"content of kind {kind.value!r} is not supported yet")
    return kind


def check_supported_role(role: Role) -> Role:
    """``role`` if this build carries it; ``UnsupportedContentError`` if not."""
    if role not in SUPPORTED_ROLES:
        raise UnsupportedContentError(f"a message of role {role.value!r} is not supported yet")
    return role


@dataclass(frozen=True, slots=True)
class TextPart:
    """Text: what a person wrote, or what a model answered."""

    text: str
    kind: ClassVar[PartKind] = PartKind.TEXT

    def __post_init__(self) -> None:
        checked_text(self.text, "the text of a part", MAX_PART_CHARS)


@dataclass(frozen=True, slots=True)
class ReasoningPart:
    """The thinking some models emit, kept apart from the answer.

    Its own kind of content, never the answer, and never sent to a vendor
    other than the one that produced it (``docs/specs/conversations.md``).
    **This version stores none of it**: an engine may stream reasoning and may
    complete an answer with a part of this kind, and the application shows the
    first and drops the second (``docs/specs/runs.md``). The record and its
    encoding are here so that the day it is kept, nothing about the format
    changes.
    """

    text: str
    kind: ClassVar[PartKind] = PartKind.REASONING

    def __post_init__(self) -> None:
        checked_text(self.text, "the text of a part", MAX_PART_CHARS)


@dataclass(frozen=True, slots=True)
class ToolCallPart:
    """A tool the model asked for: the call's id, the tool's name, the arguments.

    A part of the **assistant** message that made the call
    (``docs/specs/conversations.md``). The id is the vendor's, carried as
    data (``robinauts.domain.tools``); the name is the full name the model was
    shown, ``<prefix>__<name>``, which is how the call is routed to its server;
    and the arguments are what the model wrote, as plain data, bounded and
    checked the way ``extras`` is because they are attacker-influenced text on
    their way to a server and to a browser (``docs/specs/agents.md``).

    A copy of the arguments is kept, as ``dict`` and ``list``; it is not to
    be edited, and nothing in the platform edits it.
    """

    call_id: str
    name: str
    arguments: Mapping[str, Any] = field(default_factory=dict)
    kind: ClassVar[PartKind] = PartKind.TOOL_CALL

    def __post_init__(self) -> None:
        checked_call_id(self.call_id, "a tool call's id")
        checked_tool_name(self.name, "a tool call's name")
        object.__setattr__(
            self, "arguments", checked_data(self.arguments, "a tool call's arguments")
        )


@dataclass(frozen=True, slots=True)
class ToolResultPart:
    """What a tool answered, for one call: text, and whether it went wrong.

    One per call, in the **tool** message that answers the batch
    (``docs/specs/conversations.md``). Text in this iteration: a result of
    another kind becomes a text note saying what was left out
    (``docs/specs/agents.md``). ``is_error`` is the server saying the call
    failed, or a call that ran out of its time; the model is told either way
    and the run goes on -- only a server that cannot be reached at all fails
    a run.
    """

    call_id: str
    text: str
    is_error: bool = False
    kind: ClassVar[PartKind] = PartKind.TOOL_RESULT

    def __post_init__(self) -> None:
        checked_call_id(self.call_id, "a tool result's call id")
        checked_text(self.text, "the text of a part", MAX_PART_CHARS)
        if not isinstance(self.is_error, bool):
            raise InvalidValueError(
                f"whether a tool result is an error is yes or no, not {describe(self.is_error)}"
            )


MessagePart = TextPart | ReasoningPart | ToolCallPart | ToolResultPart
"""The closed set of content a message may hold in this version.

A union rather than a base class: adding a kind is adding a record and a name
here, and every place that takes a part apart is a match a type checker can
tell is no longer exhaustive.
"""

TOOL_PARTS = (ToolCallPart, ToolResultPart)
"""The two kinds that belong to a turn with tools, and to no user message."""


def text_parts(text: str) -> tuple[TextPart, ...]:
    """``text`` as content, split where it is longer than one part may be.

    The bound on a part is not a bound on an answer: a model asked for a very
    long one is paid for either way, so what will not fit in one part is
    carried in the next rather than cut. The split falls between code points
    -- Python counts a string in code points, so no character is ever halved
    -- and nowhere in particular otherwise: it is a bound of the store, not a
    piece of meaning.

    Here rather than in ``core`` for the same reason as ``clean_text``: what
    turns a model's answer into the platform's content is an agent adapter,
    and an adapter may import ``domain`` and must not import ``core``
    (``docs/layout.md``). The bound, the repair and the splitting are one
    subject and live together.

    Text that is not storable is refused rather than mangled; run it through
    ``clean_text`` first if it came from a provider.
    """
    if not isinstance(text, str):
        raise InvalidValueError(f"content is made of text, not {describe(text)}")
    if len(text) > MAX_MESSAGE_CHARS:
        raise InvalidValueError(
            f"one message holds at most {MAX_MESSAGE_CHARS} characters, not {len(text)}"
        )
    if not text:
        return (TextPart(""),)
    return tuple(
        TextPart(text[start : start + MAX_PART_CHARS])
        for start in range(0, len(text), MAX_PART_CHARS)
    )


def kept_parts(parts: Iterable[MessagePart]) -> tuple[MessagePart, ...]:
    """What this version stores of the content an engine produced.

    Reasoning is dropped rather than refused: an engine translates what the
    model said and is not asked to know what the platform keeps
    (``docs/specs/conversations.md``). Text and tool calls are kept. A tool
    **result** is refused: an engine never executes a tool
    (``docs/specs/agents.md``), so one that completed an answer with a result
    in it is an engine that did, and that is a bug rather than content. If
    nothing is left -- a model that only thought, or that said nothing at all
    -- what is stored is one empty piece of text, because a message must have
    content and because a conversation where the agent answered with nothing
    should say so rather than skip a turn.

    The application calls this between the engine and the store. The day
    reasoning is kept, this is where that changes.
    """
    checked = checked_parts(parts)
    if any(isinstance(part, ToolResultPart) for part in checked):
        raise InvalidValueError("an engine answers with text and tool calls, never with a result")
    kept = tuple(part for part in checked if not isinstance(part, ReasoningPart))
    return kept or (TextPart(""),)


def checked_parts(parts: object) -> tuple[MessagePart, ...]:
    """``parts`` as a bounded, non-empty tuple of the content kinds we carry.

    The one rule, used by ``Message`` and by whatever reads a message back out
    of a store, so that a row cannot hold an empty message or a thousand
    parts that nothing built in code could.
    """
    if isinstance(parts, str) or not isinstance(parts, tuple | list):
        raise InvalidValueError(f"a message's parts are a sequence, not {describe(parts)}")
    kept = tuple(parts)
    if not kept:
        raise InvalidValueError("a message has at least one part")
    if len(kept) > MAX_PARTS:
        raise InvalidValueError(f"a message has at most {MAX_PARTS} parts, not {len(kept)}")
    for part in kept:
        if not isinstance(part, MessagePart):
            raise InvalidValueError(
                f"a message holds the content kinds of this format, not {describe(part)}"
            )
    return kept


@dataclass(frozen=True, slots=True)
class Provenance:
    """What produced an answer: the agent, the engine, the model, the run.

    Every assistant message records it and the interface can show it
    (``docs/specs/conversations.md``). It is what makes a change of engine or
    of model visible after the fact: the answers of a conversation say which
    engine and which model each of them came from.
    """

    agent: str
    """The agent's id in the configuration."""
    engine: Engine
    model: str
    """The platform's own id for the model, not the vendor's name for it."""
    run_id: uuid.UUID

    def __post_init__(self) -> None:
        checked_config_id(self.agent, "an agent's id")
        checked_config_id(self.model, "a model's id")
        if not isinstance(self.engine, Engine):
            raise InvalidValueError(f"an engine is an Engine, not {describe(self.engine)}")
        checked_uuid(self.run_id, "a run's id")


@dataclass(frozen=True, slots=True)
class Message:
    """One message of a conversation: a node of the tree.

    ``parent_id`` is ``None`` for a root. A conversation has one root
    ordinarily and gains another when its first question is edited, which is
    the same as an edit anywhere else in the tree.

    Token counts are **not** recorded. Usage reporting is planned, and whether
    to keep the provider's raw counts before it exists is open
    (``docs/specs/conversations.md``, "Open"); a field nobody writes would
    decide it.

    ``created_at`` is not compared with anything. A wall clock steps backwards
    now and then -- an NTP correction is enough -- and a record that refused
    to exist because of it would lose an answer in the middle of a turn. What
    orders a conversation is the tree; time only breaks ties.
    """

    id: uuid.UUID
    conversation_id: uuid.UUID
    parent_id: uuid.UUID | None
    role: Role
    parts: tuple[MessagePart, ...]
    created_at: datetime
    channel: Channel = Channel.WEB
    provenance: Provenance | None = None
    """What produced it: on an assistant message, and on no other.

    A tool message has none: the platform wrote it, and the run that made the
    calls it answers is recorded on its parent.
    """
    extras: Mapping[str, Any] = field(default_factory=dict)
    """The vendor's opaque data, keyed by vendor; empty for nearly every message.

    Stored unread and written back as it was (``docs/specs/conversations.md``,
    "Reasoning"). A copy is kept, as plain ``dict`` and ``list``.
    """

    def __post_init__(self) -> None:
        checked_uuid(self.id, "a message's id")
        checked_uuid(self.conversation_id, "a message's conversation id")
        if self.parent_id is not None:
            checked_uuid(self.parent_id, "a message's parent id")
            if self.parent_id == self.id:
                raise InvalidValueError("a message cannot be its own parent")
        # ``is``-shaped checks, not ``==``: ``Role`` and ``Channel`` are
        # ``StrEnum``s, so a bare string reads as one and would then take
        # branches this record never checked it against.
        if not isinstance(self.role, Role):
            raise InvalidValueError(f"a message's role is a Role, not {describe(self.role)}")
        check_supported_role(self.role)
        if not isinstance(self.channel, Channel):
            raise InvalidValueError(
                f"a message's channel is a Channel, not {describe(self.channel)}"
            )
        object.__setattr__(self, "parts", checked_parts(self.parts))
        object.__setattr__(self, "extras", checked_data(self.extras, "a message's extras"))
        checked_instant(self.created_at, "created_at")
        if self.role is Role.ASSISTANT:
            if not isinstance(self.provenance, Provenance):
                raise InvalidValueError(
                    "an assistant message records the agent, engine, model and run"
                    f" that produced it, not {describe(self.provenance)}"
                )
        elif self.provenance is not None:
            raise InvalidValueError(
                f"only an assistant message has provenance, not one of role {self.role.value!r}"
            )
        _check_content_of(self.role, self.parts)

    @property
    def text(self) -> str:
        """The message's text parts, joined. Reasoning is not the answer."""
        return "".join(part.text for part in self.parts if isinstance(part, TextPart))

    @property
    def tool_calls(self) -> tuple[ToolCallPart, ...]:
        """The calls this message made, in order: on an assistant message, else none."""
        return tuple(part for part in self.parts if isinstance(part, ToolCallPart))

    @property
    def tool_results(self) -> tuple[ToolResultPart, ...]:
        """The results this message holds, in order: on a tool message, else none."""
        return tuple(part for part in self.parts if isinstance(part, ToolResultPart))


def _check_content_of(role: Role, parts: tuple[MessagePart, ...]) -> None:
    """What a message of ``role`` may hold (``docs/specs/conversations.md``).

    A user message holds no tool part; an assistant message holds no result;
    a tool message holds results and nothing else. And within one message no
    two calls, and no two results, name one call id: a result is matched to
    its call by that id, so two of one would answer nothing.
    """
    if role is Role.USER and any(isinstance(part, TOOL_PARTS) for part in parts):
        raise InvalidValueError("a user message holds no tool call and no tool result")
    if role is Role.ASSISTANT and any(isinstance(part, ToolResultPart) for part in parts):
        raise InvalidValueError("an assistant message holds no tool result; a tool message does")
    if role is Role.TOOL and not all(isinstance(part, ToolResultPart) for part in parts):
        raise InvalidValueError("a tool message holds tool results and nothing else")
    named = [part.call_id for part in parts if isinstance(part, TOOL_PARTS)]
    if len(named) != len(set(named)):
        raise InvalidValueError("a message names each tool call once")


@dataclass(frozen=True, slots=True)
class Conversation:
    """A conversation: one owner, one agent, a model, a title and its times.

    Private to its owner in this version; sharing and projects are out
    (``docs/working-notes/poc-scope.md``), and so is the trash -- deleting is
    for good -- so there are no fields for them.

    ``created_at`` and ``updated_at`` are not compared with each other, for
    the reason given on ``Message``: a clock that stepped backwards must not
    make a conversation unrecordable.
    """

    id: uuid.UUID
    owner_id: uuid.UUID
    """The user whose conversation it is. Every route checks it."""
    agent: str
    """The agent's id in the configuration; a conversation is bound to one."""
    model: str
    """The platform's id for the model its next turn runs on.

    The agent's default, copied in when the conversation starts unless its
    author picked another, and changeable at any point after
    (``docs/specs/conversations.md``). **Always there**, never "whatever the
    agent says": what a conversation runs on is read off the conversation
    alone, and a change to the agent's default reaches new conversations only.
    Checked for its spelling and nothing else -- whether the deployment still
    offers it is asked at each turn, and one it no longer does refuses that
    turn (``ModelsConfig.model_by_id``).
    """
    created_at: datetime
    updated_at: datetime
    title: str = ""
    """Empty until the first question gives it one (``core.derive_title``)."""

    def __post_init__(self) -> None:
        checked_uuid(self.id, "a conversation's id")
        checked_uuid(self.owner_id, "a conversation's owner")
        checked_config_id(self.agent, "an agent's id")
        checked_config_id(self.model, "a model's id")
        checked_line(self.title, "a conversation's title", MAX_TITLE_CHARS)
        checked_instant(self.created_at, "created_at")
        checked_instant(self.updated_at, "updated_at")
