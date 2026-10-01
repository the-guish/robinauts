# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Ids a test knows in advance, so it can say what a stored row's id is."""

from __future__ import annotations

import uuid

from robinauts.legacy.ports import IdSource


class CountingIdSource(IdSource):
    """``00000000-0000-4000-8000-000000000001``, then ``...02``: in order, and real.

    Version 4 and variant 1 in the bits that say so, because everything
    downstream is a ``uuid.UUID`` and nothing may be exercised against a shape
    no real source has. ``given`` holds them in order, for a test that wants
    the id of the third thing it made.
    """

    def __init__(self) -> None:
        self._count = 0
        self.given: list[uuid.UUID] = []

    def new_id(self) -> uuid.UUID:
        self._count += 1
        made = uuid.UUID(f"00000000-0000-4000-8000-{self._count:012d}")
        self.given.append(made)
        return made
