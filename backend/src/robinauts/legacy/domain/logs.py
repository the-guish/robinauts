# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The one way text nobody here wrote is written to a log.

A log is a file of lines, and a line ends at a newline. A path, a header, a
provider's error message, the message of an exception raised inside a
framework: each of them is whatever somebody else chose, and written out as
they are they do not go **into** a line -- they make new ones, of whatever
shape the sender picked, in the middle of the record of what the deployment
did.

So everything of that kind goes through ``shown``: **escaped**, with
``ascii()``, so that nothing coming out of it can end a line or start one, and
**bounded**, because the length is the sender's choice too and a log that can
be filled a megabyte at a time is a log that can be made to lose what came
before. ``chain`` and ``where`` are the same rule applied to an exception: what
it and its causes said, and the frames of the innermost one, in one line.

**In ``domain`` because both sides need it.** ``api`` writes what a request
carried (``robinauts.legacy.api.logs``, which re-exports these); the application
writes what an engine or a store raised, which is as much somebody else's text
as a request is. It is pure -- a string in, a string out -- and depends on
nothing (``docs/layout.md``).
"""

from __future__ import annotations

import traceback

MAX_SHOWN = 120
"""How much of a value from outside a log line repeats.

Enough to recognise a path or an origin; far too little to bury the line
before it. What is dropped is counted, so a line never quietly says less than
it means to.
"""

MAX_LOGGED = 600
"""How much of a message written **by us** a log line carries.

Longer than ``MAX_SHOWN``: these are written for an operator, and a
configuration error lists every problem it found. Bounded all the same,
because some of them are built out of a request or out of what a provider
said.
"""

MAX_CAUSES = 5
"""How far down a chain of causes a log line follows before it stops."""

MAX_FRAMES = 15
"""How many frames of the innermost cause a log line carries.

The last ones, which are the ones nearest what went wrong. Enough to find the
line; not a page of stack for every refusal.
"""


def shown(value: object, *, most: int = MAX_SHOWN) -> str:
    """``value`` as a log line may carry it: quoted, escaped and bounded.

    The clipping happens **before** the escaping, so the bound is on the text
    that arrived rather than on its spelling; an escape is up to six
    characters, so what comes out is longer than ``most`` and still bounded by
    it. What was dropped is counted rather than left to be guessed at.
    """
    text = value if isinstance(value, str) else str(value)
    clipped = text[:most]
    escaped = ascii(clipped)
    if len(clipped) == len(text):
        return escaped
    return f"{escaped}+{len(text) - len(clipped)} more"


def chain(exc: BaseException, *, most: int = MAX_LOGGED) -> str:
    """``exc`` and what raised it, each escaped and bounded, in one line.

    An error of ours often says only what kind of thing went wrong -- "a
    stored message cannot be read by this build" is the whole of it, on
    purpose, because the browser must learn nothing from it. Which message,
    and what exactly was wrong with it, is in the error it was raised **from**,
    and without that an operator is told that something is broken and nothing
    about what. So the chain is followed and written out.

    Escaped and bounded like anything else a log line carries: the deepest
    cause is frequently the one built out of a row or out of what a provider
    said, and that is as much somebody's text as a request is.
    """
    said = []
    seen: BaseException | None = exc
    while seen is not None and len(said) < MAX_CAUSES:
        said.append(f"{type(seen).__name__}: {shown(seen, most=most)}")
        seen = seen.__cause__ or seen.__context__
    return " <- ".join(said)


def where(exc: BaseException, *, most: int = MAX_FRAMES) -> str:
    """Where the innermost cause was raised: ``file:line in function``.

    A 5xx of ours says nothing to the browser and often little to the log
    either -- "a stored message cannot be read by this build" is a sentence
    about a kind of thing, and ``reading_stored`` gives that same sentence to
    a bug in our own reader. Without the frames there is nothing to look at.

    **Only the frames**, and only their three fields: no source lines, no
    locals, and not the exception's own message, which ``chain`` carries and
    escapes. A file name and a function name are the interpreter's, not
    anybody's input, and they go through ``shown`` all the same, so that
    nothing here can end a log line or start one.
    """
    innermost: BaseException = exc
    seen = 0
    while (innermost.__cause__ or innermost.__context__) and seen < MAX_CAUSES:
        innermost = innermost.__cause__ or innermost.__context__  # type: ignore[assignment]
        seen += 1
    frames = traceback.extract_tb(innermost.__traceback__)[-most:]
    return shown(
        " < ".join(f"{frame.filename}:{frame.lineno} in {frame.name}" for frame in frames),
        most=MAX_LOGGED,
    )
