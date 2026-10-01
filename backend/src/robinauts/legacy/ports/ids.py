# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Where a new id comes from.

A port for four lines of ``uuid`` because of where the ids are needed: the
application builds a conversation, a message and a run **before** any of them
is stored, since each names the others, and the layer that holds the platform's
pure rules may have no randomness in it at all (``docs/layout.md``). So the one
call that invents an id is behind a seam, and
a test replaces it with a source that counts, which is what lets a test say
what a stored row's id is without reading it back.

It is deliberately not part of ``SecretSource``: an id is public, goes in a
URL and is stored in the clear, and a source of ids that a test can predict
must never be mistaken for a source of secrets that one could.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod


class IdSource(ABC):
    """A fresh id for something the platform is about to store."""

    @abstractmethod
    def new_id(self) -> uuid.UUID:
        """An id no row of this deployment has.

        Synchronous: making one touches nothing outside the process, and a
        coroutine here would make every caller of it one too.
        """
        raise NotImplementedError
