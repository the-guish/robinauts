# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Where a real id comes from: ``uuid.uuid4``.

Version 4 and nothing else. A conversation's id goes in a URL and is handed to
whoever asks for the conversation, so an id must say nothing about when it was
made or on which machine -- which is what a version 1 uuid says, and a good
part of why nothing here uses one. Being random is also what lets the
application mint an id without asking the database, which is what the port
exists for.

It is **not** a source of secrets: an id is public, and
``robinauts.legacy.adapters.secrets`` is the one thing in the platform whose output
is meant to be unguessable.
"""

from __future__ import annotations

import uuid

from robinauts.legacy.ports import IdSource


class OsIdSource(IdSource):
    """Ids from ``uuid.uuid4``, which draws on the operating system's randomness.

    Stateless, and therefore safe to share: every call asks again, and there
    is no counter, no seed and no way to make it repeat.
    """

    def new_id(self) -> uuid.UUID:
        """A fresh version 4 uuid."""
        return uuid.uuid4()
