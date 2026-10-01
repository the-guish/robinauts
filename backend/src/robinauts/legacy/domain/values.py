# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The checks the platform's records share, written once.

A record validates itself in ``__post_init__`` (``docs/working-notes/
poc-progress.md``), and the conversation format's records all have to ask the
same questions: is this an aware datetime, is this really a ``UUID``, is this
text of a size we are willing to keep, and is it text a database and a UTF-8
stream can actually carry. They are here so that the answer is one answer, and
so that a value read back out of a store is checked exactly as one built in
code.

**No refusal repeats the value it refused.** What is named is the field, the
rule and what kind of thing arrived (``describe``) -- never its contents. A
message is whatever a person typed or a provider sent, it can be a megabyte
long, and an error message is copied into logs, tracebacks and crash reports.

**Text that can be stored.** A NUL is not storable in a PostgreSQL ``text``
column and a lone surrogate cannot be encoded as UTF-8: both would be accepted
here and then fail at the one moment a message must not be lost, on the way to
the database. So the format refuses them, and ``clean_text`` is the repair the
layer that meets them applies first -- **joining back** a pair that arrived in
two pieces before it replaces what is still half a character.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Mapping
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

from robinauts.legacy.domain.errors import InvalidValueError

EARLIEST_YEAR = 1970
"""The first year anything the platform records may be dated in."""

LATEST_YEAR = 9998
"""The last one.

A year below or above this cannot be turned into UTC at all: an offset would
push it past what a datetime can hold. The bound is here, on the record,
rather than only where a record is written down: a row that cannot be encoded
is a row that cannot be read back, and the place to refuse one is where it is
built.
"""

MAX_PART_CHARS = 1_000_000
"""The longest one piece of content may be, and one delta with it.

Generous on purpose. A model asked for its longest answer can return a few
hundred thousand characters, and a bound that cut one would throw away an
answer that has already been paid for. Text longer than this is carried as
several parts (``robinauts.legacy.domain.text_parts``) or several deltas
(``publishable``), never lost.
"""

NUL = "\x00"
"""What no stored text may hold: PostgreSQL's ``text`` cannot carry it."""

REPLACEMENT = "�"
"""What a lone surrogate is repaired to: the replacement character."""

MAX_EXTRAS_BYTES = 64 * 1024
"""How big the extras of one message, or the arguments of one tool call, may be.

The UTF-8 of the canonical JSON, written as it would be written, so that the
bound is the same 64 KiB in every script rather than four times smaller in
one that escapes. Bounded like everything else the platform keeps: without
it, the one key nobody reads is the one place anything at all can be stored
(``docs/specs/conversations.md``, "The version").
"""

MAX_EXTRAS_DEPTH = 32
"""How deeply such data may nest.

Measured **before** anything is written out, by a walk that does not recurse:
a twenty-thousand-deep object would otherwise be a ``RecursionError`` out of
the encoder, which is neither a refusal nor a message anyone can act on.
"""

MAX_EXTRAS_NODES = 4096
"""How many values such data may hold, counted in the same walk."""

JOINERS = frozenset({"‍", "︎", "️"})
"""Zero-width joiner and the variation selectors.

Not printable by Python's reckoning, and part of the text all the same: they
are what holds an emoji sequence together, so a line that has them is one line
and dropping them would take a family apart into its members.
"""

_SURROGATE = re.compile("[\ud800-\udfff]")
"""The code points UTF-8 has no encoding for; Python holds them all the same."""


def describe(value: object) -> str:
    """What ``value`` is, for a message that must not repeat what it says."""
    if value is None:
        return "nothing"
    if isinstance(value, str):
        return f"text of {len(value)} characters"
    if isinstance(value, bytes | bytearray):
        return f"{len(value)} bytes"
    named = type(value).__name__
    article = "an" if named[:1].lower() in "aeiou" else "a"
    if isinstance(value, list | tuple | set | frozenset | dict):
        return f"{article} {named} of {len(value)}"
    return f"{article} {named}"


def checked_aware(when: object, what: str) -> datetime:
    """``when`` if it is an aware datetime; ``InvalidValueError`` if it is not.

    A naive datetime would be read as local time by one comparison and as UTC
    by the next, and a conversation ordered two ways is not ordered.
    """
    if not isinstance(when, datetime) or when.tzinfo is None or when.utcoffset() is None:
        raise InvalidValueError(f"{what} must be an aware datetime, not {describe(when)}")
    return when


def checked_instant(when: object, what: str) -> datetime:
    """``when`` if it is an aware datetime the platform can write down.

    Aware, and within ``EARLIEST_YEAR``..``LATEST_YEAR`` **once it is UTC**,
    which is how every time is written (``robinauts.legacy.core.instant``): a time
    one minute inside the window at +14:00 is outside it converted, and a
    record that cannot be encoded is one nothing can read back.
    """
    moment = checked_aware(when, what)
    try:
        utc = moment.astimezone(UTC)
    except (OverflowError, ValueError, OSError):
        raise InvalidValueError(
            f"{what} is between the years {EARLIEST_YEAR} and {LATEST_YEAR} once it is UTC"
        ) from None
    if not EARLIEST_YEAR <= utc.year <= LATEST_YEAR:
        raise InvalidValueError(
            f"{what} is between the years {EARLIEST_YEAR} and {LATEST_YEAR} once it is UTC,"
            f" not {utc.year}"
        )
    return moment


def checked_uuid(value: object, what: str) -> uuid.UUID:
    """``value`` if it is a ``UUID``; ``InvalidValueError`` if it is not.

    The string spelling of an id is refused rather than parsed: a ``str`` and
    a ``UUID`` of the same id compare unequal, so one that slipped in would
    make a message the child of a parent nothing can find.
    """
    if not isinstance(value, uuid.UUID):
        raise InvalidValueError(f"{what} is a UUID, not {describe(value)}")
    return value


def checked_fragment(value: object, what: str, limit: int) -> str:
    """``value`` if it is text of at most ``limit`` characters.

    For a piece of a message still being produced. Everything the platform
    keeps is bounded -- a message, an error, a title -- because what a model
    or a browser sent is as long as it liked until something says otherwise,
    and this is that something.
    """
    if not isinstance(value, str):
        raise InvalidValueError(f"{what} is text, not {describe(value)}")
    if len(value) > limit:
        raise InvalidValueError(f"{what} is at most {limit} characters, not {len(value)}")
    return value


def checked_text(value: object, what: str, limit: int) -> str:
    """``value`` if it is bounded text a database and a UTF-8 stream can hold.

    Bounded, with no NUL and no lone surrogate. ``clean_text`` is what turns
    text that is not into text that is.
    """
    text = checked_fragment(value, what, limit)
    if NUL in text:
        raise InvalidValueError(f"{what} cannot hold a NUL character")
    if _SURROGATE.search(text) is not None:
        raise InvalidValueError(f"{what} cannot hold an unpaired surrogate")
    return text


def is_storable(text: str) -> bool:
    """Whether ``text`` is text a database and a UTF-8 stream can carry."""
    return isinstance(text, str) and NUL not in text and _SURROGATE.search(text) is None


def clean_text(text: str) -> str:
    """``text`` made storable: pairs joined, what is left of them replaced.

    **A split pair is put back together first.** A provider streams where it
    likes, so one character above the basic plane can arrive as its high half
    and then its low half; joining the two is what this is for, and replacing
    both would lose a character that did arrive whole. The round trip through
    UTF-16 is that join: a valid pair becomes the one character it spells, and
    a half with nothing beside it comes back as it was.

    **What is left is lossy, by necessity.** A surrogate still on its own is
    half a character that never arrived and there is nothing to restore it
    from; a NUL cannot be stored at all. Losing them is better than losing the
    message they are in, which is what refusing the message further down would
    come to.

    Whoever turns what a provider sent into the platform's own content calls
    this first, which is an agent adapter -- and an adapter may import
    ``domain`` and must not import ``core`` (``docs/layout.md``). So the
    repair lives beside the rule it repairs to, where both of its callers can
    reach it. Linear in the length of the text.
    """
    if not isinstance(text, str):
        raise InvalidValueError(f"text to clean is text, not {describe(text)}")
    text = text.replace(NUL, "")
    if _SURROGATE.search(text) is None:
        return text
    joined = text.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "surrogatepass")
    return _SURROGATE.sub(REPLACEMENT, joined)


def publishable(carry: str, fragment: str) -> tuple[tuple[str, ...], str]:
    """What of ``fragment`` can be published now, and what must wait.

    An engine's fragments are whatever a provider sent; what the platform
    publishes is **storable text**, since it is written into the run's events
    and sent over the wire. Turning the one into the other has to be done in
    one place, because the order of the three things it does is the whole of
    its correctness:

    1. a NUL is dropped -- it cannot be stored, and it can arrive **between**
       the two halves of a character, where leaving it would keep them apart;
    2. a trailing high half is held back, so that the low half arriving in
       the next fragment finds it;
    3. what is still half a character in what is left is replaced.

    ``carry`` is what was held back last time, ``""`` to begin with. What
    comes back is the **pieces** to publish -- none of them empty, each
    within ``MAX_PART_CHARS``, which is nearly always one, and two only when
    a held-back half made a fragment at the bound one character too long for
    a single delta:

    .. code-block:: python

        pieces, carry = publishable(carry, fragment.text)
        for piece in pieces:
            publish(TextPiece(..., text=piece))
        ...
        last = flush(carry)   # at the end of the answer

    **The promise**: everything published, joined, plus the flush, is exactly
    ``clean_text`` of everything the engine sent. What a person watched
    arrive is what is stored, character for character.
    """
    text = carry + fragment.replace(NUL, "")
    if text and "\ud800" <= text[-1] <= "\udbff":
        text, carry = text[:-1], text[-1]
    else:
        carry = ""
    # Sliced after cleaning, so no piece can end on half a character: there
    # are none left to halve.
    cleaned = clean_text(text)
    return (
        tuple(
            cleaned[start : start + MAX_PART_CHARS]
            for start in range(0, len(cleaned), MAX_PART_CHARS)
        ),
        carry,
    )


def flush(carry: str) -> str:
    """What is left of an answer once there is no more of it to come.

    A half that never found its other half; ``""`` if nothing was held back.
    """
    return clean_text(carry)


def checked_data(
    data: object,
    what: str,
    *,
    max_bytes: int = MAX_EXTRAS_BYTES,
    max_depth: int = MAX_EXTRAS_DEPTH,
    max_nodes: int = MAX_EXTRAS_NODES,
) -> dict[str, Any]:
    """``data`` if it is a bounded object of plain data; ``InvalidValueError`` if not.

    **The shape the format reserves for ``extras``**, and the shape a tool
    call's arguments have (``docs/specs/conversations.md``): an object whose
    keys are names, holding strings, numbers, booleans, ``None``, lists and
    objects of the same, storable throughout, nesting no deeper than
    ``max_depth``, holding no more than ``max_nodes`` values, and no bigger
    than ``max_bytes`` once written as canonical JSON. Nothing in it is read
    for meaning here; what is checked is that a store can hold it and an
    export can write it.

    The **shape** is measured first, by a walk of its own, because measuring
    the size means writing it out and writing out a deep enough object is a
    ``RecursionError`` -- an error nobody can act on, from a place that
    promised a refusal. The walk uses a stack rather than the stack, and it
    checks the text as it goes, key and value at every depth: a NUL cannot be
    stored in a ``jsonb`` column and a lone surrogate cannot be encoded at
    all, and this is the walk that is already looking at every one of them.

    What comes back is a **copy**, as plain ``dict`` and ``list``: a mapping
    of another kind (a ``MappingProxyType``, say) and a tuple are taken and
    written as an object and a list, and a caller that goes on editing what it
    passed edits nothing of the record's.
    """
    if not isinstance(data, Mapping):
        raise InvalidValueError(f"{what} is an object, not {describe(data)}")
    nodes = 0
    stack: list[tuple[object, int]] = [(data, 1)]
    while stack:
        value, depth = stack.pop()
        nodes += 1
        if depth > max_depth:
            raise InvalidValueError(f"{what} nests at most {max_depth} deep")
        if nodes > max_nodes:
            raise InvalidValueError(f"{what} holds at most {max_nodes} values")
        if isinstance(value, str):
            if not is_storable(value):
                raise InvalidValueError(f"the text in {what} is storable, at every depth")
        elif isinstance(value, Mapping):
            for key in value:
                if not isinstance(key, str):
                    raise InvalidValueError(f"the keys of {what} are names, at every depth")
                if not is_storable(key):
                    raise InvalidValueError(f"the text in {what} is storable, at every depth")
            stack.extend((inside, depth + 1) for inside in value.values())
        elif isinstance(value, list | tuple):
            stack.extend((inside, depth + 1) for inside in value)
        elif value is not None and not isinstance(value, bool | int | float):
            raise InvalidValueError(
                f"{what} is plain data an export can write, not {describe(value)}"
            )
    try:
        # ``allow_nan=False``: ``NaN`` and the infinities are no part of JSON
        # (RFC 8259) and Python writes them as an extension nothing else
        # reads back. ``ensure_ascii=False``: the bound is on the UTF-8 this
        # would really be written as -- and the encoding is inside the guard,
        # because that is the step that can fail.
        written = json.dumps(
            data,
            separators=(",", ":"),
            allow_nan=False,
            ensure_ascii=False,
            default=_as_object,
        )
        size = len(written.encode("utf-8"))
    except (TypeError, ValueError, UnicodeError):
        raise InvalidValueError(f"{what} is plain data an export can write") from None
    if size > max_bytes:
        raise InvalidValueError(f"{what} is at most {max_bytes} bytes written out, not {size}")
    # Through JSON and back rather than ``deepcopy`` alone: what is kept is
    # exactly what would be written, as ``dict`` and ``list``, whatever
    # mapping or sequence types the caller built it from.
    return deepcopy(json.loads(written))


def _as_object(value: object) -> dict[str, Any]:
    """A mapping of another kind, as the object JSON writes; anything else is refused.

    ``json.dumps`` asks this for a value it cannot write itself. The walk in
    ``checked_data`` has already let only mappings, sequences and scalars
    through, so what arrives here is a mapping that is not a ``dict``; the
    ``TypeError`` for anything else is the refusal ``checked_data`` catches.
    """
    if isinstance(value, Mapping):
        return dict(value)
    raise TypeError(f"{describe(value)} is not plain data")


def checked_line(value: object, what: str, limit: int) -> str:
    """``value`` if it is bounded text on one line, printable throughout.

    A title is shown in a list and in a tab; a newline, a tab or a control
    character in one is a display it never recovers from, and is refused
    where the record is built rather than where it is drawn. The joiners of
    ``JOINERS`` are kept: they are inside words, not between lines.
    """
    text = checked_text(value, what, limit)
    if any(
        not (character.isprintable() or character == " " or character in JOINERS)
        for character in text
    ):
        raise InvalidValueError(f"{what} is one line of printable text")
    return text
