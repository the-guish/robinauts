# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The two ``IdSource`` implementations, against the contract both must meet.

``OsIdSource`` is what a deployment runs on and ``CountingIdSource`` is what a
test runs on, and the suite is the same for both. The fake's own promise --
that it is **predictable**, which is why a test uses it -- is the two tests at
the bottom, and it is a promise about the sequence, not an excuse from being a
uuid.
"""

from __future__ import annotations

import uuid

from contracts.ids import IdSourceContract
from fakes import CountingIdSource
from robinauts.legacy.adapters import OsIdSource
from robinauts.legacy.ports import IdSource


class TestOsIdSource(IdSourceContract):
    """What the deployment draws ids from."""

    def new_source(self) -> IdSource:
        return OsIdSource()


class TestCountingIdSource(IdSourceContract):
    """And what a test draws them from: the same shape, a known order."""

    def new_source(self) -> IdSource:
        return CountingIdSource()


def test_the_fake_counts_so_a_test_knows_what_it_was_given() -> None:
    source = CountingIdSource()

    made = [source.new_id() for _ in range(3)]

    assert made == [uuid.UUID(f"00000000-0000-4000-8000-{n:012d}") for n in (1, 2, 3)]
    assert source.given == made


def test_two_fakes_hand_out_the_same_sequence() -> None:
    # Which is what lets a test say the id of the third thing it made without
    # reading it back, and is exactly what the real source must never do.
    one, other = CountingIdSource(), CountingIdSource()

    assert [one.new_id() for _ in range(5)] == [other.new_id() for _ in range(5)]


def test_the_real_source_does_not_repeat_across_instances() -> None:
    assert OsIdSource().new_id() != OsIdSource().new_id()
