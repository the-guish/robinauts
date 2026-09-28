# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Tools: what a tool is called, and how a call is named (``docs/specs/agents.md``).

The part of the tool vocabulary the conversation format needs first: the
spelling of a tool's name and of a call's id, which a stored ``ToolCallPart``
carries and a ``ToolResultPart`` answers. The definitions a run is handed and
the servers an operator configures join this module with the steps that
build them (``docs/working-notes/mcp-plan.md``).

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

from robinauts.domain.errors import InvalidValueError
from robinauts.domain.values import checked_line, describe

MAX_TOOL_NAME_CHARS = 64
"""The longest a tool's full name may be: the bound the vendors share."""

_TOOL_NAME = re.compile(rf"[A-Za-z0-9_-]{{1,{MAX_TOOL_NAME_CHARS}}}")
"""What the vendors accept a tool name to be spelt with."""

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


def checked_call_id(value: object, what: str) -> str:
    """``value`` if it is an id a call can be matched by; ``InvalidValueError`` if not.

    One line of printable text with no blanks in it, bounded: the least a
    vendor's id is, and the most a store, a wire and a client's key need.
    """
    text = checked_line(value, what, MAX_CALL_ID_CHARS)
    if not text or any(character.isspace() for character in text):
        raise InvalidValueError(f"{what} is one word of printable text, not {describe(value)}")
    return text
