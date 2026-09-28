# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Tools: what a tool is called, how a call is named, what a run is handed.

The spelling of a tool's name and of a call's id, which a stored
``ToolCallPart`` carries and a ``ToolResultPart`` answers; ``ListedTool``,
the tool as a server lists it, under the server's own name; ``ToolDefinition``,
the tool as the model is shown it, under the full name the platform gave it --
the list a run fetches once from the servers its agent names and hands the
agent port for the whole turn (``docs/specs/agents.md``, "Tools"); and
``ToolResult``, what one call came back with. The servers an operator
configures are ``ToolServerConfig`` (``robinauts.domain.agents``); the
naming, from listed to shown, is ``robinauts.core.tools``.

**A tool's name is the vendors' bound, not ours.** Anthropic and OpenAI both
take a tool name of at most 64 characters matching ``^[a-zA-Z0-9_-]+$``, and
that is the platform's bound on the **full** name a model is shown --
``<prefix>__<name>``, the server's prefix and the tool's own name -- because a
name a vendor would refuse is a turn that fails at the first model call.

**A call's id is the vendor's**, chosen by the model and carried by the engine
as data (``docs/specs/agents.md``, "The agent port"): the platform stores it
on the call, the result names it, the wire sends it as it is, and the adapter
replays it unchanged. So the rule here is only what every vendor's id and
every store can hold -- bounded, one line, printable, no blanks -- and not
one vendor's spelling.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from robinauts.domain.errors import InvalidValueError
from robinauts.domain.values import (
    MAX_PART_CHARS,
    checked_data,
    checked_line,
    checked_text,
    describe,
)

MAX_TOOL_NAME_CHARS = 64
"""The longest a tool's full name may be: the bound the vendors share."""

_TOOL_NAME = re.compile(rf"[A-Za-z0-9_-]{{1,{MAX_TOOL_NAME_CHARS}}}")
"""What the vendors accept a tool name to be spelt with."""

TOOL_NAME_SEPARATOR = "__"
"""What joins a server's prefix to a tool's own name in the name the model sees.

Two underscores, which a prefix may not hold (``is_tool_prefix``), so that the
first occurrence in a full name is the join and the server is read off the
name alone (``docs/specs/agents.md``, "Tools").
"""

MAX_TOOL_PREFIX_CHARS = 32
"""The longest a server's prefix may be.

Half the vendors' bound on the full name, so that a real tool name -- thirty
characters, past ``list_pull_request_reviews`` -- fits after any prefix an
operator may write. A server's id (``MAX_CONFIG_ID_CHARS``) can be longer
than this, which is why a long id needs a prefix written down.
"""

_TOOL_PREFIX = re.compile(rf"[A-Za-z0-9_-]{{1,{MAX_TOOL_PREFIX_CHARS}}}")
"""What a prefix is spelt with: the vendors' own charset, so that it fits the name."""

MAX_CALL_ID_CHARS = 128
"""The longest a call's id may be.

Generous next to what any vendor sends -- a few dozen characters -- and a
bound all the same, because the id is stored on every call and result, sent
over the wire and used as a key by the client.
"""


def is_tool_name(value: object) -> bool:
    """Whether ``value`` is a tool name the vendors would take."""
    return isinstance(value, str) and _TOOL_NAME.fullmatch(value) is not None


def checked_tool_name(value: object, what: str) -> str:
    """``value`` if it is a tool name the vendors would take; ``InvalidValueError`` if not."""
    if not is_tool_name(value):
        raise InvalidValueError(
            f"{what} is letters, digits, '_' and '-', at most {MAX_TOOL_NAME_CHARS} of them,"
            f" not {describe(value)}"
        )
    return value


def is_tool_prefix(value: object) -> bool:
    """Whether ``value`` may be a server's prefix: a name that can be told apart.

    The vendors' charset, bounded; never holding the separator, so that the
    join is the first ``__`` of a full name; and never ending in ``_``, so
    that ``a_`` + ``__b`` and ``a`` + ``___b`` are not one name spelt twice.
    """
    return (
        isinstance(value, str)
        and _TOOL_PREFIX.fullmatch(value) is not None
        and TOOL_NAME_SEPARATOR not in value
        and not value.endswith("_")
    )


def checked_tool_prefix(value: object, what: str) -> str:
    """``value`` if it may be a server's prefix; ``InvalidValueError`` if not."""
    if not is_tool_prefix(value):
        raise InvalidValueError(
            f"{what} is letters, digits, '_' and '-', at most {MAX_TOOL_PREFIX_CHARS} of them,"
            f" holding no '{TOOL_NAME_SEPARATOR}' and not ending in '_', not {describe(value)}"
        )
    return value


def checked_call_id(value: object, what: str) -> str:
    """``value`` if it is an id a call can be matched by; ``InvalidValueError`` if not.

    One line of printable text with no blanks in it, bounded: the least a
    vendor's id is, and the most a store, a wire and a client's key need.
    """
    text = checked_line(value, what, MAX_CALL_ID_CHARS)
    if not text or any(character.isspace() for character in text):
        raise InvalidValueError(f"{what} is one word of printable text, not {describe(value)}")
    return text


MAX_TOOL_DESCRIPTION_CHARS = 20_000
"""The longest a tool's description may be.

A server writes it and a model reads it on every call of the turn, so it is
bounded like everything the platform carries; twenty thousand characters is
far past any description a vendor would want in a prompt.
"""

MAX_TOOL_SCHEMA_BYTES = 256 * 1024
"""How big a tool's input schema may be, written as canonical JSON.

Larger than ``extras`` and a call's arguments (``MAX_EXTRAS_BYTES``): a
schema is the server's whole description of what a tool takes, nested
objects and enumerations included, and it is sent to the model rather than
stored. Bounded all the same, since it is attacker-influenced text on its
way into a prompt.
"""


MAX_LISTED_TOOL_NAME_CHARS = 256
"""The longest a server's own name for a tool may be, as listed.

Not the vendors' bound (``MAX_TOOL_NAME_CHARS``), which the **full** name is
held to when the platform names the tool (``robinauts.core.tools``): a server
may list a name of any spelling, and one the vendors would not take is left
out of a run's list rather than refused at the port.
"""


@dataclass(frozen=True, slots=True)
class ListedTool:
    """One tool as a server lists it: its own name, what it does, what it takes.

    What the ``ToolServers`` port hands back for what a server lists, before
    the platform has named it: ``name`` is the server's, one line of text; the
    rest is what ``ToolDefinition`` carries, bounded the same way, because it
    is the same attacker-influenced text on its way to a prompt. Copies of the
    two mappings are kept; nothing edits them.
    """

    name: str
    description: str = ""
    input_schema: Mapping[str, Any] = field(default_factory=dict)
    annotations: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        checked_line(self.name, "a listed tool's name", MAX_LISTED_TOOL_NAME_CHARS)
        if not self.name.strip():
            raise InvalidValueError("a listed tool has a name")
        checked_text(self.description, "a tool's description", MAX_TOOL_DESCRIPTION_CHARS)
        object.__setattr__(
            self,
            "input_schema",
            checked_data(
                self.input_schema, "a tool's input schema", max_bytes=MAX_TOOL_SCHEMA_BYTES
            ),
        )
        object.__setattr__(
            self, "annotations", checked_data(self.annotations, "a tool's annotations")
        )


@dataclass(frozen=True, slots=True)
class ToolResult:
    """What one call came back with: text, and whether the server calls it a failure.

    The port's answer to a call (``robinauts.ports.ToolServers``), before
    the platform has stored it as the ``ToolResultPart`` answering the
    call: the same text and the same flag, bounded the same way
    (``MAX_PART_CHARS``), because that is where it goes. ``is_error`` is the
    server saying the call failed, or a call that ran out of its time; a
    server that cannot be reached at all is not a result but a
    ``ToolServerError`` (``docs/specs/runs.md``, "Tools").
    """

    text: str
    is_error: bool = False

    def __post_init__(self) -> None:
        checked_text(self.text, "a tool result's text", MAX_PART_CHARS)
        if not isinstance(self.is_error, bool):
            raise InvalidValueError(
                f"whether a tool result is an error is yes or no, not {describe(self.is_error)}"
            )


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """One tool as the model is shown it: its full name, what it does, what it takes.

    ``name`` is the **full** name, ``<prefix>__<name>`` -- the server's prefix
    and the tool's own -- which is how a call is routed to its server from the
    name alone (``docs/specs/agents.md``, "Tools"). ``input_schema`` is the
    JSON Schema the server declared, as plain data; the engines hand it to the
    vendor as it is. ``annotations`` are MCP's hints about the tool
    (``readOnlyHint``, ``destructiveHint`` and the rest), carried and read by
    nothing yet: they are what an approval policy would read
    (``docs/specs/runs.md``, "Tools").

    Copies of the two mappings are kept, as ``dict`` and ``list``; nothing
    edits them.
    """

    name: str
    description: str
    input_schema: Mapping[str, Any]
    annotations: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        checked_tool_name(self.name, "a tool's name")
        checked_text(self.description, "a tool's description", MAX_TOOL_DESCRIPTION_CHARS)
        object.__setattr__(
            self,
            "input_schema",
            checked_data(
                self.input_schema, "a tool's input schema", max_bytes=MAX_TOOL_SCHEMA_BYTES
            ),
        )
        object.__setattr__(
            self, "annotations", checked_data(self.annotations, "a tool's annotations")
        )


NO_RESULT = (
    "no result of this call was recorded: the turn that made it ended before one came, and"
    " whether the call ran is not known"
)
"""What a model is told of a call that no tool message answers.

A stored answer that asked for tools and has no tool message under it on the
path -- the turn was stopped, or failed, before its results were in
(``docs/specs/runs.md``, "Tools") -- would go to the vendor as calls with
nothing answering them, which the vendors refuse: a call is followed by its
result or the request is refused whole. So each agent adapter puts, after such
an answer, the tool turn the vendor requires -- one **error** result per
unanswered call, saying this -- which is what the record says and no more:
the platform runs a batch in parallel and writes the tool message when the
last result is in, so a call without one may have run, and the sentence
neither says it did nor invites the model to make it again. It is **never
stored**: the record keeps the calls without results, which is what happened,
and the client shows exactly that. One sentence, the same under both engines,
so that a conversation moved across the swap is told the same thing about
the same call -- a choice of the adapters', not a rule the swap test holds
them to. Which calls those are is ``unanswered_calls``
(``robinauts.domain.conversation``).
"""
