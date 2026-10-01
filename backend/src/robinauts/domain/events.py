# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""What an agent adapter streams for one turn, in a form no framework defines.

The event set of `agent-framework-examples
<https://github.com/the-guish/agent-framework-examples>`_ (``common/events.py``
on ``feature/event-streaming``), which is the contract the adapters are
built against: coarse on purpose, and the same five events whichever
framework produced them. The framework runs the whole turn, tools included;
the adapter only translates what it emits (``docs/specs/agents.md``).

- ``TextDelta``: more of the answer's text.
- ``ReasoningDelta``: more of the model's thinking. The one event the
  examples do not carry yet -- their README names thinking as the next thing
  a chat interface needs -- and this interface already shows it.
- ``ToolCall``: the model asked for a tool and the framework is about to run
  it, with its arguments once they are known.
- ``ToolResult``: what the tool came back with, as the model will see it.
- ``Done``: the turn is over, with the final answer and the framework's
  **state** after it -- the conversation in the framework's own format,
  which the platform stores unread and hands back at the next turn
  (``docs/specs/conversations.md``, "The model's memory").

No ids of the platform's, no times, no positions: an adapter has none of
those. The application turns these into the platform's own messages and
numbered run events (``robinauts.domain.turn``), which is where a message's
id, its parent and its row come from.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from robinauts.domain.conversation import MAX_PART_CHARS
from robinauts.domain.errors import InvalidValueError
from robinauts.domain.tools import checked_call_id, checked_tool_name
from robinauts.domain.values import checked_data, checked_fragment, checked_line, describe

MAX_CHECKPOINT_ID_CHARS = 256
"""How long the id of a checkpoint an engine hands back may be.

Opaque to the platform, which stores it on the answer that ended the turn
and hands it back to the same engine, and bounded because it is written
into a message's document (``robinauts.ports.agent_engine``). Generous for
what the engines use -- a LangGraph checkpoint id is a 36-character UUID, a
Pydantic AI snapshot id is one the adapter mints -- and a bound all the same.
"""

MAX_ENGINE_STATE_BYTES = 64 * 1024 * 1024
"""How big the state a framework hands back may be.

Generous: a framework's history of a long conversation with large tool
results, before its own context management has folded it. Bounded all the
same, because it is written into the database on every finished turn, and a
state past this is a conversation nothing could send to a model anyway.
"""


@dataclass(frozen=True, slots=True)
class TextDelta:
    """More of the answer's text.

    Any text at all, a half of a character included -- a provider splits where
    it likes, and what is storable is decided once the pieces are joined
    (``robinauts.domain.clean_text``).
    """

    text: str

    def __post_init__(self) -> None:
        checked_fragment(self.text, "a delta's text", MAX_PART_CHARS)


@dataclass(frozen=True, slots=True)
class ReasoningDelta:
    """More of the thinking behind the answer being produced.

    An adapter may yield these and is never required to. The application
    publishes them as they arrive and stores none of it as content
    (``docs/specs/conversations.md``, "Reasoning").
    """

    text: str

    def __post_init__(self) -> None:
        checked_fragment(self.text, "a delta's text", MAX_PART_CHARS)


@dataclass(frozen=True, slots=True)
class ToolCall:
    """The model asked for a tool, and the framework is about to run it.

    ``call_id`` is the vendor's, carried as data; ``name`` is the tool's name
    as the framework showed it to the model; ``arguments`` are whole -- the
    frameworks announce a call once its arguments are known. A copy of the
    arguments is kept, bounded as a call's arguments are on a message.
    """

    call_id: str
    name: str
    arguments: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        checked_call_id(self.call_id, "a tool call's id")
        checked_tool_name(self.name, "a tool call's name")
        object.__setattr__(
            self, "arguments", checked_data(self.arguments, "a tool call's arguments")
        )


@dataclass(frozen=True, slots=True)
class ToolResult:
    """What a tool came back with, as the model will see it.

    ``output`` is text, bounded as a result is on a message; ``is_error`` is
    the framework saying the call failed, which the model is told and the
    transcript records (``robinauts.domain.ToolResultPart``).
    """

    call_id: str
    name: str
    output: str
    is_error: bool = False

    def __post_init__(self) -> None:
        checked_call_id(self.call_id, "a tool call's id")
        checked_tool_name(self.name, "a tool's name")
        checked_fragment(self.output, "a tool result's output", MAX_PART_CHARS)
        if not isinstance(self.is_error, bool):
            raise InvalidValueError(
                f"whether a tool result is an error is yes or no, not {describe(self.is_error)}"
            )


@dataclass(frozen=True, slots=True)
class Done:
    """The turn is over. ``text`` is the final answer; the rest names the memory after it.

    ``text`` is what ``ask`` returns in the examples, and what the transcript
    stores of the last answer when nothing of it was streamed (``docs/specs/agents.md``).

    ``checkpoint_id`` is for the ``AgentEngine`` port
    (``robinauts.ports.agent_engine``): the engine's own id for the memory as
    it is at the end of this turn, which the platform stores on the answer,
    never reads, and hands back to the same engine to continue after or to
    fork at. ``None`` for an engine that keeps no memory.

    ``state`` is for the ``Agent`` port it replaces (``robinauts.ports.agents``):
    the conversation as the framework keeps it after this turn, serialised by
    the framework's own means, which the platform stores on the run and
    never reads; ``None`` for an adapter that keeps nothing. It goes with
    that port.
    """

    text: str
    state: bytes | None = None
    checkpoint_id: str | None = None

    def __post_init__(self) -> None:
        checked_fragment(self.text, "an answer's text", MAX_PART_CHARS)
        if self.checkpoint_id is not None:
            if not isinstance(self.checkpoint_id, str) or not self.checkpoint_id:
                raise InvalidValueError(
                    f"a checkpoint id is non-empty text, not {describe(self.checkpoint_id)}"
                )
            checked_line(self.checkpoint_id, "a checkpoint id", MAX_CHECKPOINT_ID_CHARS)
        if self.state is not None:
            if not isinstance(self.state, bytes):
                raise InvalidValueError(f"a framework's state is bytes, not {describe(self.state)}")
            if len(self.state) > MAX_ENGINE_STATE_BYTES:
                raise InvalidValueError(
                    f"a framework's state is at most {MAX_ENGINE_STATE_BYTES} bytes,"
                    f" not {len(self.state)}"
                )


Event = TextDelta | ReasoningDelta | ToolCall | ToolResult | Done
"""Everything an agent adapter yields, as a closed set.

Deliberately not the same records as the platform's published events
(``robinauts.domain.turn.TurnEvent``): every one of those carries an id of
something only the application knows about.
"""
