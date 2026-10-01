# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The in-memory conversation store, against the contract every store must meet.

And the contract itself, against a store that is this same one **with its lock
taken away**. That is the whole difference: it validates the same things,
refuses the same things and writes the same rows, and the only promise it
cannot keep is that an operation is indivisible. So the tests it fails say
something precise, and the test at the bottom says exactly which ones they are
-- a set, not a sample, so a concurrency test that stopped biting, or a plain
test that started depending on atomicity, is noticed here.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest

from contracts.conversation_runs import ConversationRunsContract
from fakes import MemoryConversationStore
from robinauts.legacy.ports import ConversationStore


class MemoryStoreContract(ConversationRunsContract):
    """The whole contract -- conversations, messages, runs, events -- over the fake."""

    async def new_store(self) -> ConversationStore:
        return MemoryConversationStore()

    async def dump(self, store: ConversationStore) -> str:
        # Field by field, not repr: a repr leaves out what a dataclass hides.
        assert isinstance(store, MemoryConversationStore)
        return store.everything()


class TestMemoryConversationStore(MemoryStoreContract):
    """The fake passes what the PostgreSQL store will be held to."""


class RacyConversationStore(MemoryConversationStore):
    """The fake, with nothing holding an operation together.

    One override, and it is the lock. Every method still reads, still checks
    and still writes exactly as the honest store does -- and each of them
    still awaits between the read and the write, which under the lock
    interleaves nothing and without it is where another caller gets in. This
    is what a store looks like when its statements are not in a transaction.
    """

    @asynccontextmanager
    async def _locked(self) -> AsyncIterator[None]:
        yield


class RacyContract(MemoryStoreContract):
    """The whole contract, over the racy store. Not collected: it is run by hand."""

    async def new_store(self) -> ConversationStore:
        return RacyConversationStore()


NOT_ATOMIC = {
    "test_a_rename_and_an_append_at_once_keep_both",
    "test_a_model_change_and_an_append_at_once_keep_both",
    "test_one_of_several_runs_started_at_once_is_the_one_that_runs",
    "test_one_of_several_events_offered_a_position_at_once_is_stored",
    "test_a_delete_and_a_run_beginning_at_once_leave_no_orphan",
    "test_two_completions_offered_a_position_at_once_store_one_whole_answer",
    "test_a_completion_and_an_end_at_once_leave_a_readable_stream",
    "test_a_rename_and_a_completion_at_once_keep_both",
    "test_a_snapshot_taken_while_a_message_completes_holds_both_or_neither",
    "test_a_snapshot_taken_while_a_run_ends_shows_it_going_or_not_at_all",
}
"""Every test of the suite that a store without a transaction fails.

The rest pass against it, which is the point: one caller at a time, it is a
correct store, and that is exactly how such a store reaches production.

The last two are the compound **read**: a snapshot is held to what the
compound writes are, so a store that reads its four parts one after another is
caught by the same device -- the tests slide the read across the write at every
interleaving of a single-threaded loop, and one of them falls between two
statements.

**This set is exact here and nowhere else.** Over this store the schedule is
the whole of what can happen, so each of these fails every time and every
other test passes every time. Over a real database the same tests are attempts
at a race whose timing belongs to the server: a correct store passes them
always, a torn one may pass them by luck, and no such set could be asserted.
That is why the device lives beside the fake -- it is the place where "the
concurrency tests have teeth" is something a test can prove.

``test_messages_appended_at_once_...`` is deliberately **not** here. Appends
of different messages touch different rows and each writes its own pair of
conversation fields, so a store that writes them in one statement keeps that
invariant with or without a transaction; the test pins the invariant, and what
catches a store that has lost it is the rename beside an append, where two
writers really do meet on one row.
"""


def _checks() -> list[str]:
    """Every test the contract defines, whichever half of it defines them."""
    return sorted(name for name in dir(RacyContract) if name.startswith("test_"))


def test_the_contract_covers_the_tests_the_racy_store_is_judged_on() -> None:
    # So that a renamed or deleted concurrency test cannot quietly leave the
    # set below naming nothing.
    assert NOT_ATOMIC <= set(_checks())


def test_a_store_without_a_transaction_fails_exactly_the_tests_about_two_at_once() -> None:
    contract = RacyContract()
    failed = set()
    for name in _checks():
        try:
            getattr(contract, name)()
        except AssertionError:
            failed.add(name)
    assert failed == NOT_ATOMIC


@pytest.mark.parametrize("check", sorted(NOT_ATOMIC))
def test_each_of_them_fails_on_its_own_too(check: str) -> None:
    # The same thing said one test at a time, so that a failure names which.
    with pytest.raises(AssertionError):
        getattr(RacyContract(), check)()
