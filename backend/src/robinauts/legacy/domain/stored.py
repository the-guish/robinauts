# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Reading our own rows is not reading a request.

A row of ours that cannot be read is a fault of the deployment: a build older
than the version it was written in, a kind of content this one does not carry,
a column of the wrong type, a conversation that is no tree. None of it is
something the browser asked for, and none of it is anything the browser should
be told: it answers as a fault of ours, saying nothing, with the whole of it --
and what it was raised from -- in the log (``robinauts.legacy.api.errors``).

``reading_stored`` is how that is done, and it is **the** way a store turns
its own columns into the flat records it owns them for -- ``Conversation``,
``Run``, ``User``:

.. code-block:: python

    with reading_stored("a stored conversation cannot be read by this build"):
        return Conversation(
            id=row["id"],
            owner_id=row["owner_id"],
            agent=row["agent"],
            model=row["model"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            title=row["title"],
        )

It is here, in ``domain``, because a store may import ``domain`` and must not
import ``core`` (``docs/layout.md``). That is also why a store never builds a
**message** or a **run event**: those are written in the platform's own
format, the format is ``core``'s, and a store that parsed one would be
deciding what a message means. It is handed the **document** -- the plain
mapping ``core`` wrote -- keeps it as it is, and fills its own indexed columns
from the record beside it. The application encodes before it writes and
decodes what it reads, through ``core``'s ``*_stored`` readers, which are this
same wrapping with the decoding already done.

**What it converts**: ``InvalidValueError`` and everything under it --
``InvalidMessageTreeError``, ``UnsupportedContentError``,
``UnsupportedFormatError`` -- and the four ways a row of the wrong shape
breaks the code that reads it: a ``KeyError`` for a column that is not there,
a ``TypeError`` or an ``AttributeError`` for a value that is not what it
should be, a ``ValueError`` from parsing one -- and a ``RecursionError``,
which is what a row nested deeply enough does to whatever walks it. A row
that cannot be taken apart is stored data too, and the store is the last
place that can say so.

It converts **our own bugs in that code as well**, and that is deliberate: a
``TypeError`` from a reader of ours is no more something the browser did than
a bad row is, and both answer as a fault of ours. What locates it is the log,
which carries the chain and the frames of the innermost cause
(``robinauts.legacy.api.errors``).

**What passes through**: everything else. A ``NotFoundError`` -- an id a
request named and that is not there says nothing about the rows. Every error
of the database driver, none of which is one of the shapes above: a
connection that dropped is not a row that cannot be read, and it is answered
and retried as what it is. And ``CancelledError``, which is a
``BaseException`` and never caught here: a cancelled request must stop, not
be told that its rows are broken.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from robinauts.legacy.domain import StoredDataError


@contextmanager
def reading_stored(what: str) -> Iterator[None]:
    """Whatever is wrong with our own rows is ``StoredDataError``, chained.

    ``what`` is the whole of what anyone outside is told, so it names the kind
    of row and nothing of it. What was really wrong is the cause, which the
    log follows.
    """
    try:
        yield
    except (KeyError, TypeError, AttributeError, ValueError, RecursionError) as cause:
        # ``InvalidValueError`` is a ``ValueError``, so it and everything
        # under it are here too.
        raise StoredDataError(what) from cause
