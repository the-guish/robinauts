# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""What every ``ConversationStore`` must do: conversations, messages, order.

The first half of **one** suite over **one** store. The runs and the events of
that same store are in ``contracts/conversation_runs.py``, whose class extends
this one; splitting them is about the length of a module and nothing else, and
a store is certified by the class that inherits both.

Subclass and override ``new_store`` to return an empty store, as
``contracts/credential_store.py`` is subclassed and for the same reasons: it
is a coroutine, awaited inside the test's own event loop, so a store that
opens connections opens them there. **Every call of it must give an empty
store of its own** -- a database-backed one, a schema of its own -- because
some tests open several in turn and a second that found the first's rows
would be testing something else.

The in-memory fake passes this now; the PostgreSQL store is held to the same
suite when it lands, which is the point of writing it here rather than beside
the fake.

Two kinds of promise are checked. The plain ones -- what is stored, what comes
back, in what order -- and the ones about **two things happening at once**.
Those are what a store built out of a read and a later write quietly breaks,
and a suite without them certifies such a store as correct;
``tests/unit/test_fake_conversation_store.py`` keeps them honest by running
the whole suite against a store that differs from the fake in atomicity alone
and requiring **exactly** the concurrency tests to fail.

What this asks of an implementation:

- **it is called concurrently** and must stay correct when it is: a pool, or
  a connection per call.
- **a message and the conversation move together.** ``append_message``
  stores the document and advances ``updated_at`` in one indivisible step.
  Two ``UPDATE``s, or an ``UPDATE`` that writes back a row it read a moment
  ago, lose whatever else was written in between -- a rename, most obviously.
- **the listing has a total order.** ``updated_at`` descending is not enough:
  two conversations updated in one millisecond would swap between two pages,
  so the id breaks the tie and the keyset carries both halves.
- **a document is kept whole.** What goes in comes out equal, and neither the
  caller's mapping nor the one handed back is the stored one.
- **a parent that is not a message of the conversation is refused**, so a
  dangling parent cannot be stored whatever a caller believed.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime

import pytest

from aio import asyncio_test
from conversations import (
    CONVERSATION,
    OTHER_CONVERSATION,
    OWNER,
    answer,
    at,
    conversation,
    question,
)
from robinauts.core import message_from_data, message_to_data
from robinauts.domain import (
    MAX_TITLE_CHARS,
    Conversation,
    ConversationNotFoundError,
    InvalidValueError,
    Message,
    MessageNotFoundError,
    Role,
    TextPart,
    ToolCallPart,
    ToolResultPart,
)
from robinauts.ports import MAX_PAGE, ConversationStore

SOMEBODY_ELSE = uuid.UUID("55555555-5555-4555-8555-555555555555")
"""Another person, whose conversations are never in this one's listing."""


class ConversationStoreContract:
    """Subclass this and override ``new_store`` to return an empty store."""

    async def new_store(self) -> ConversationStore:
        """An empty store, built inside the event loop the test runs on."""
        raise NotImplementedError("a ConversationStoreContract subclass overrides `new_store`")

    async def close_store(self, store: ConversationStore | None) -> None:
        """Let go of whatever ``new_store`` took. Nothing, unless it took something.

        ``None`` when ``new_store`` did not finish, which is why it has to
        cope with half of one.
        """

    @asynccontextmanager
    async def opened(self) -> AsyncIterator[ConversationStore]:
        """An empty store for one test, closed however the test ends."""
        store: ConversationStore | None = None
        try:
            store = await self.new_store()
            yield store
        finally:
            await self.close_store(store)

    async def dump(self, store: ConversationStore) -> str:
        """Everything the store holds, as text: every row, **every field**.

        There is no way through the port to ask a store what is left after a
        delete, and "it is gone" is the promise worth checking from outside. A
        subclass says how: the in-memory one writes out each field of each
        record and each document, a database one selects every column of every
        row of every table of its own.
        """
        raise NotImplementedError("a ConversationStoreContract subclass overrides `dump`")

    # Conversations.

    @asyncio_test
    async def test_a_conversation_is_stored_as_it_is_and_found_by_id(self) -> None:
        async with self.opened() as store:
            kept = conversation()

            await store.add_conversation(kept)

            assert await store.conversation_by_id(kept.id) == kept

    @asyncio_test
    async def test_a_conversation_keeps_its_model_through_every_read_and_write(self) -> None:
        # The model is the conversation's, not the agent's (docs/specs/agents.md):
        # one other than the default goes in and comes back out of every path a
        # conversation is read by, and a rename or a touch leaves it alone.
        async with self.opened() as store:
            await store.add_conversation(conversation(model="gpt-5-5"))

            found = await store.conversation_by_id(CONVERSATION)
            listed = await store.conversations_of(OWNER, limit=10)
            snapshot = await store.conversation_snapshot(CONVERSATION)
            renamed = await store.rename_conversation(CONVERSATION, "Mine", now=at(30))
            touched = await store.touch_conversation(CONVERSATION, now=at(31))

            assert found is not None and found.model == "gpt-5-5"
            assert [each.model for each in listed.conversations] == ["gpt-5-5"]
            assert snapshot.conversation is not None
            assert snapshot.conversation.model == "gpt-5-5"
            assert renamed is not None and renamed.model == "gpt-5-5"
            assert touched is not None and touched.model == "gpt-5-5"

    @asyncio_test
    async def test_an_id_nobody_stored_finds_nothing_rather_than_refusing(self) -> None:
        async with self.opened() as store:
            assert await store.conversation_by_id(OTHER_CONVERSATION) is None
            assert await store.messages_of(OTHER_CONVERSATION) == ()

    @asyncio_test
    async def test_an_id_already_stored_is_refused_rather_than_overwritten(self) -> None:
        async with self.opened() as store:
            await store.add_conversation(conversation())

            with pytest.raises(InvalidValueError):
                await store.add_conversation(conversation(title="a second one"))

            found = await store.conversation_by_id(CONVERSATION)
            assert found is not None and found.title == "What is a robinaut?"

    # Listing.

    @asyncio_test
    async def test_conversations_are_listed_most_recently_updated_first(self) -> None:
        async with self.opened() as store:
            for seconds in (0, 20, 10):
                await store.add_conversation(
                    conversation(id=uuid.uuid4(), updated_at=at(seconds), title=f"at {seconds}")
                )

            page = await store.conversations_of(OWNER, limit=10)

            assert [kept.title for kept in page.conversations] == ["at 20", "at 10", "at 0"]
            assert page.cursor is None

    @asyncio_test
    async def test_a_listing_holds_nobody_elses_conversations(self) -> None:
        async with self.opened() as store:
            await store.add_conversation(conversation())
            await store.add_conversation(
                conversation(id=OTHER_CONVERSATION, owner_id=SOMEBODY_ELSE)
            )

            mine = await store.conversations_of(OWNER, limit=10)
            theirs = await store.conversations_of(SOMEBODY_ELSE, limit=10)

            assert [kept.id for kept in mine.conversations] == [CONVERSATION]
            assert [kept.id for kept in theirs.conversations] == [OTHER_CONVERSATION]

    @asyncio_test
    async def test_a_page_hands_back_where_the_next_one_begins(self) -> None:
        async with self.opened() as store:
            for seconds in range(5):
                await store.add_conversation(
                    conversation(id=uuid.uuid4(), updated_at=at(seconds), title=f"at {seconds}")
                )

            seen = await _paged(store, OWNER, limit=2)

            assert [kept.title for kept in seen] == [f"at {seconds}" for seconds in (4, 3, 2, 1, 0)]

    @asyncio_test
    async def test_paging_is_stable_when_conversations_share_a_time(self) -> None:
        # The tie is broken by id, so no conversation is shown twice and none
        # is skipped -- which is what a listing ordered by the time alone does
        # the moment two rows share one.
        async with self.opened() as store:
            made = [conversation(id=uuid.uuid4(), updated_at=at(3)) for _ in range(6)]
            for kept in made:
                await store.add_conversation(kept)

            seen = await _paged(store, OWNER, limit=2)

            assert [kept.id for kept in seen] == sorted((kept.id for kept in made), reverse=True)

    @asyncio_test
    async def test_a_cursor_that_does_not_parse_is_refused(self) -> None:
        async with self.opened() as store:
            await store.add_conversation(conversation())

            for nonsense in ("", "1", "not a cursor", f"{at(0)}|not-a-uuid"):
                with pytest.raises(InvalidValueError):
                    await store.conversations_of(OWNER, limit=10, cursor=nonsense)

    @asyncio_test
    async def test_one_persons_cursor_shows_another_person_nothing_of_theirs(self) -> None:
        # A cursor is a position inside the caller's **own** listing. It is
        # not signed and does not have to be: carried to somebody else it
        # moves a window over their conversations and can widen it onto
        # nobody's.
        async with self.opened() as store:
            for seconds in range(4):
                await store.add_conversation(
                    conversation(id=uuid.uuid4(), updated_at=at(seconds), title=f"mine {seconds}")
                )
            for seconds in range(4):
                await store.add_conversation(
                    conversation(
                        id=uuid.uuid4(),
                        owner_id=SOMEBODY_ELSE,
                        updated_at=at(seconds),
                        title=f"theirs {seconds}",
                    )
                )
            mine = await store.conversations_of(OWNER, limit=2)
            assert mine.cursor is not None

            theirs = await store.conversations_of(SOMEBODY_ELSE, limit=10, cursor=mine.cursor)
            again = await store.conversations_of(SOMEBODY_ELSE, limit=10)

            assert all(kept.owner_id == SOMEBODY_ELSE for kept in theirs.conversations)
            assert all(kept.title.startswith("theirs") for kept in theirs.conversations)
            # And their own listing is untouched by having been asked with it.
            assert [kept.title for kept in again.conversations] == [
                f"theirs {seconds}" for seconds in (3, 2, 1, 0)
            ]

    @asyncio_test
    async def test_a_page_of_no_size_and_a_page_of_every_size_are_refused(self) -> None:
        async with self.opened() as store:
            for limit in (0, -1, MAX_PAGE + 1):
                with pytest.raises(InvalidValueError):
                    await store.conversations_of(OWNER, limit=limit)

    # Renaming.

    @asyncio_test
    async def test_renaming_gives_the_title_and_dates_the_conversation(self) -> None:
        async with self.opened() as store:
            await store.add_conversation(conversation())

            written = await store.rename_conversation(CONVERSATION, "Mine", now=at(30))

            assert written is not None
            assert (written.title, written.updated_at) == ("Mine", at(30))
            # What it hands back is what it wrote, not what a caller must
            # rebuild out of the record it read a moment before.
            assert await store.conversation_by_id(CONVERSATION) == written

    @asyncio_test
    async def test_a_title_no_conversation_could_hold_is_refused(self) -> None:
        # A title is one bounded, printable line -- `Conversation` says so,
        # and a store that took anything else would hold a row the record
        # cannot be built from, or hand the caller a driver error where the
        # port promises a refusal. The check runs before the write, so the
        # conversation keeps the title it had.
        async with self.opened() as store:
            await store.add_conversation(conversation())

            for nonsense in (
                None,
                7,
                "a title\nover two lines",
                "a title with a \x00 in it",
                "x" * (MAX_TITLE_CHARS + 1),
            ):
                with pytest.raises(InvalidValueError):
                    await store.rename_conversation(
                        CONVERSATION,
                        nonsense,  # type: ignore[arg-type]
                        now=at(5),
                    )

            found = await store.conversation_by_id(CONVERSATION)
            assert found is not None
            assert (found.title, found.updated_at) == ("What is a robinaut?", at(0))

    @asyncio_test
    async def test_renaming_what_is_not_there_says_so_and_stores_nothing(self) -> None:
        async with self.opened() as store:
            assert await store.rename_conversation(CONVERSATION, "Mine", now=at(30)) is None
            assert await store.touch_conversation(CONVERSATION, now=at(30)) is None
            assert await store.conversation_by_id(CONVERSATION) is None

    # Changing the model.

    @asyncio_test
    async def test_changing_the_model_gives_it_dates_it_and_changes_nothing_else(
        self,
    ) -> None:
        async with self.opened() as store:
            kept = conversation()
            await store.add_conversation(kept)

            written = await store.set_model(CONVERSATION, "gpt-5-5", now=at(30))

            # Everything else -- the owner, the agent, the engine, the title
            # and when it began -- is what it was.
            assert written == replace(kept, model="gpt-5-5", updated_at=at(30))
            assert await store.conversation_by_id(CONVERSATION) == written
            listed = await store.conversations_of(OWNER, limit=10)
            assert listed.conversations == (written,)

    @asyncio_test
    async def test_the_model_changes_again_and_a_rename_leaves_the_last_one(self) -> None:
        async with self.opened() as store:
            await store.add_conversation(conversation())

            await store.set_model(CONVERSATION, "gpt-5-5", now=at(10))
            await store.set_model(CONVERSATION, "gemini-2-5-pro", now=at(20))
            renamed = await store.rename_conversation(CONVERSATION, "Mine", now=at(30))

            assert renamed is not None
            assert (renamed.model, renamed.title) == ("gemini-2-5-pro", "Mine")
            assert await store.conversation_by_id(CONVERSATION) == renamed

    @asyncio_test
    async def test_a_model_no_conversation_could_hold_is_refused(self) -> None:
        # A model is named by a configuration id, and the check runs before the
        # write, so the conversation keeps the model and the time it had. That
        # the deployment offers the model is not the store's to know.
        async with self.opened() as store:
            kept = conversation()
            await store.add_conversation(kept)

            for nonsense in (None, 7, "", "GPT 5.5", "gpt\n5", "a \x00 in it", "x" * 1000):
                with pytest.raises(InvalidValueError):
                    await store.set_model(
                        CONVERSATION,
                        nonsense,  # type: ignore[arg-type]
                        now=at(5),
                    )

            assert await store.conversation_by_id(CONVERSATION) == kept

    @asyncio_test
    async def test_changing_the_model_of_what_is_not_there_says_so_and_stores_nothing(
        self,
    ) -> None:
        async with self.opened() as store:
            await store.add_conversation(conversation())
            assert await store.delete_conversation(CONVERSATION, now=at(9))

            assert await store.set_model(CONVERSATION, "gpt-5-5", now=at(30)) is None
            assert await store.set_model(OTHER_CONVERSATION, "gpt-5-5", now=at(30)) is None
            assert await store.conversation_by_id(CONVERSATION) is None
            assert await store.conversation_by_id(OTHER_CONVERSATION) is None

    @asyncio_test
    async def test_changing_one_conversations_model_leaves_the_others(self) -> None:
        async with self.opened() as store:
            other = conversation(id=OTHER_CONVERSATION)
            await store.add_conversation(conversation())
            await store.add_conversation(other)

            await store.set_model(CONVERSATION, "gpt-5-5", now=at(30))

            assert await store.conversation_by_id(OTHER_CONVERSATION) == other

    # Touching, and the time.

    @asyncio_test
    async def test_touching_dates_the_conversation_and_changes_nothing_else(self) -> None:
        async with self.opened() as store:
            kept = conversation()
            await store.add_conversation(kept)

            written = await store.touch_conversation(CONVERSATION, now=at(30))

            assert written == replace(kept, updated_at=at(30))
            assert await store.conversation_by_id(CONVERSATION) == written

    @asyncio_test
    async def test_a_naive_time_names_no_instant_and_is_refused(self) -> None:
        naive = datetime(2026, 9, 21, 12, 0)
        async with self.opened() as store:
            await store.add_conversation(conversation())
            first = question()

            with pytest.raises(InvalidValueError):
                await store.rename_conversation(CONVERSATION, "Mine", now=naive)
            with pytest.raises(InvalidValueError):
                await store.set_model(CONVERSATION, "gpt-5-5", now=naive)
            with pytest.raises(InvalidValueError):
                await store.touch_conversation(CONVERSATION, now=naive)
            with pytest.raises(InvalidValueError):
                await store.append_message(first, message_to_data(first), now=naive)
            with pytest.raises(InvalidValueError):
                await store.delete_conversation(CONVERSATION, now=naive)

    # Messages.

    @asyncio_test
    async def test_a_message_document_is_kept_whole_and_handed_back_as_it_was(self) -> None:
        async with self.opened() as store:
            await store.add_conversation(conversation())
            first = question("Is it kept?")
            document = message_to_data(first)

            await store.append_message(first, document, now=at(1))

            assert list(await store.messages_of(CONVERSATION)) == [document]

    @asyncio_test
    async def test_a_turn_with_tools_is_kept_whole_and_read_back(self) -> None:
        """An answer with calls and a vendor's extras, and the tool message under
        it, are documents like any other: the store keeps them as they are and
        the format reads them back equal (``docs/specs/conversations.md``)."""
        async with self.opened() as store:
            await store.add_conversation(conversation())
            asked = question("look it up", seconds=0)
            calling = answer(
                asked,
                parts=(
                    TextPart("Let me look."),
                    ToolCallPart("toolu_01", "github__search", {"q": "x"}),
                ),
                extras={"anthropic": {"thinking": [{"signature": "sig", "thinking": "hm"}]}},
                seconds=1,
            )
            answered = Message(
                id=uuid.uuid4(),
                conversation_id=CONVERSATION,
                parent_id=calling.id,
                role=Role.TOOL,
                parts=(ToolResultPart("toolu_01", "found 3", is_error=False),),
                created_at=at(2),
            )
            for message, when in ((asked, at(0)), (calling, at(1)), (answered, at(2))):
                await store.append_message(message, message_to_data(message), now=when)

            documents = await store.messages_of(CONVERSATION)

            assert [message_from_data(document) for document in documents] == [
                asked,
                calling,
                answered,
            ]
            assert documents[1]["extras"] == calling.extras
            assert documents[2]["role"] == "tool"

    @asyncio_test
    async def test_appending_dates_the_conversation(self) -> None:
        async with self.opened() as store:
            await store.add_conversation(conversation())
            first = question()

            await _appended(store, first, at(7))

            found = await store.conversation_by_id(CONVERSATION)
            assert found is not None
            assert found.updated_at == at(7)

    @asyncio_test
    async def test_appending_to_a_conversation_that_is_not_there_is_refused(self) -> None:
        async with self.opened() as store:
            stray = question()

            with pytest.raises(ConversationNotFoundError):
                await store.append_message(stray, message_to_data(stray), now=at(1))

    @asyncio_test
    async def test_a_message_id_already_stored_is_refused(self) -> None:
        async with self.opened() as store:
            await store.add_conversation(conversation())
            first = question()
            await _appended(store, first, at(1))

            with pytest.raises(InvalidValueError):
                await _appended(store, first, at(2))

            assert len(await store.messages_of(CONVERSATION)) == 1

    @asyncio_test
    async def test_a_message_id_is_taken_for_the_whole_deployment(self) -> None:
        # As a primary key is. The same id offered in another conversation is
        # the same row, and a store that scoped it per conversation would let
        # one id name two messages.
        async with self.opened() as store:
            await store.add_conversation(conversation())
            await store.add_conversation(conversation(id=OTHER_CONVERSATION))
            first = question()
            await _appended(store, first, at(1))
            elsewhere = question(conversation_id=OTHER_CONVERSATION, id=first.id)

            with pytest.raises(InvalidValueError):
                await _appended(store, elsewhere, at(2))

            assert await store.messages_of(OTHER_CONVERSATION) == ()

    @asyncio_test
    async def test_a_parent_that_is_no_message_of_the_conversation_is_not_found(self) -> None:
        # So a dangling parent can never be stored, whatever the caller
        # believed about the tree.
        async with self.opened() as store:
            await store.add_conversation(conversation())
            await store.add_conversation(conversation(id=OTHER_CONVERSATION))
            elsewhere = question(conversation_id=OTHER_CONVERSATION)
            await _appended(store, elsewhere, at(1))

            for parent in (elsewhere.id, uuid.uuid4()):
                with pytest.raises(MessageNotFoundError):
                    await _appended(store, question(parent=parent), at(2))

            assert await store.messages_of(CONVERSATION) == ()

    @asyncio_test
    async def test_messages_come_back_oldest_first(self) -> None:
        async with self.opened() as store:
            await store.add_conversation(conversation())
            first = question(seconds=0)
            second = answer(first, seconds=1)
            third = question("And again?", parent=second, seconds=2)
            for message in (first, second, third):
                await _appended(store, message, at(9))

            documents = await store.messages_of(CONVERSATION)

            assert [document["id"] for document in documents] == [
                str(message.id) for message in (first, second, third)
            ]

    @asyncio_test
    async def test_one_conversations_messages_are_not_anothers(self) -> None:
        async with self.opened() as store:
            await store.add_conversation(conversation())
            await store.add_conversation(conversation(id=OTHER_CONVERSATION))
            mine = question()
            theirs = question(conversation_id=OTHER_CONVERSATION)
            await _appended(store, mine, at(1))
            await _appended(store, theirs, at(1))

            assert [document["id"] for document in await store.messages_of(CONVERSATION)] == [
                str(mine.id)
            ]

    @asyncio_test
    async def test_what_a_caller_holds_is_never_what_the_store_holds(self) -> None:
        # In and out. A store over a database copies both ways because the
        # document goes to the server and comes back; one that handed out its
        # own mapping would let a caller edit the database by editing a dict.
        async with self.opened() as store:
            await store.add_conversation(conversation())
            first = question("As written")
            document = message_to_data(first)
            await store.append_message(first, document, now=at(1))

            document["parts"][0]["text"] = "as mutated afterwards"
            (read,) = await store.messages_of(CONVERSATION)
            read["parts"][0]["text"] = "as mutated on the way out"

            (again,) = await store.messages_of(CONVERSATION)
            assert again["parts"][0]["text"] == "As written"

    @asyncio_test
    async def test_a_snapshot_of_a_conversation_with_no_run_is_its_messages(self) -> None:
        async with self.opened() as store:
            await store.add_conversation(conversation())
            first = question()
            await _appended(store, first, at(1))

            snapshot = await store.conversation_snapshot(CONVERSATION)

            assert snapshot.conversation is not None
            assert [document["id"] for document in snapshot.messages] == [str(first.id)]
            assert (snapshot.active_run, snapshot.events) == (None, ())

    @asyncio_test
    async def test_a_snapshot_of_a_conversation_that_is_not_there_is_empty(self) -> None:
        async with self.opened() as store:
            snapshot = await store.conversation_snapshot(CONVERSATION)

            assert snapshot.conversation is None
            assert (snapshot.messages, snapshot.active_run, snapshot.events) == ((), None, ())

    # Deleting.

    @asyncio_test
    async def test_deleting_removes_the_conversation_and_every_message_of_it(self) -> None:
        async with self.opened() as store:
            await store.add_conversation(conversation())
            await store.add_conversation(conversation(id=OTHER_CONVERSATION))
            first = question("What is deleted?")
            await _appended(store, first, at(1))
            kept = question("What is kept?", conversation_id=OTHER_CONVERSATION)
            await _appended(store, kept, at(1))

            assert await store.delete_conversation(CONVERSATION, now=at(9))

            assert await store.conversation_by_id(CONVERSATION) is None
            assert await store.messages_of(CONVERSATION) == ()
            held = await self.dump(store)
            assert str(CONVERSATION) not in held
            assert str(first.id) not in held
            assert "What is deleted?" not in held
            # And the conversation beside it is untouched.
            assert "What is kept?" in held

    @asyncio_test
    async def test_deleting_what_is_not_there_says_so(self) -> None:
        async with self.opened() as store:
            assert not await store.delete_conversation(CONVERSATION, now=at(9))

    # Two things at once.

    @asyncio_test
    async def test_a_rename_and_an_append_at_once_keep_both(self) -> None:
        # The one a store built out of "read the row, write the row back"
        # loses: whichever of the two wrote second overwrites what the first
        # had just written, and the title or the time disappears.
        async with self.opened() as store:
            await store.add_conversation(conversation())
            first = question()

            await asyncio.gather(
                store.rename_conversation(CONVERSATION, "Renamed", now=at(5)),
                store.append_message(first, message_to_data(first), now=at(6)),
            )

            found = await store.conversation_by_id(CONVERSATION)
            assert found is not None
            assert found.title == "Renamed"
            assert found.updated_at in {at(5), at(6)}
            assert len(await store.messages_of(CONVERSATION)) == 1

    @asyncio_test
    async def test_a_model_change_and_an_append_at_once_keep_both(self) -> None:
        # The rename's race, over the other column a person changes: the model
        # chosen for the next turn must not be lost to an append that wrote
        # back the row it read, nor the append's time to the change.
        async with self.opened() as store:
            await store.add_conversation(conversation())
            first = question()

            await asyncio.gather(
                store.set_model(CONVERSATION, "gpt-5-5", now=at(5)),
                store.append_message(first, message_to_data(first), now=at(6)),
            )

            found = await store.conversation_by_id(CONVERSATION)
            assert found is not None
            assert found.model == "gpt-5-5"
            assert found.updated_at in {at(5), at(6)}
            assert len(await store.messages_of(CONVERSATION)) == 1

    @asyncio_test
    async def test_messages_appended_at_once_are_all_there_and_one_of_them_dates_it(
        self,
    ) -> None:
        async with self.opened() as store:
            await store.add_conversation(conversation())
            root = question(seconds=0)
            await _appended(store, root, at(0))
            together = [answer(root, f"at once {n}", seconds=n + 1) for n in range(5)]

            await asyncio.gather(
                *(
                    store.append_message(message, message_to_data(message), now=at(n + 1))
                    for n, message in enumerate(together)
                )
            )

            documents = await store.messages_of(CONVERSATION)
            assert len(documents) == len(together) + 1
            found = await store.conversation_by_id(CONVERSATION)
            assert found is not None
            # Dated by one of the appends, whichever wrote last.
            assert found.updated_at in {at(n + 1) for n in range(len(together))}

    @asyncio_test
    async def test_two_conversations_written_to_at_once_do_not_mix(self) -> None:
        async with self.opened() as store:
            await store.add_conversation(conversation())
            await store.add_conversation(conversation(id=OTHER_CONVERSATION))
            mine = question("mine")
            theirs = question("theirs", conversation_id=OTHER_CONVERSATION)

            await asyncio.gather(
                store.append_message(mine, message_to_data(mine), now=at(1)),
                store.append_message(theirs, message_to_data(theirs), now=at(2)),
            )

            assert [document["id"] for document in await store.messages_of(CONVERSATION)] == [
                str(mine.id)
            ]
            assert [document["id"] for document in await store.messages_of(OTHER_CONVERSATION)] == [
                str(theirs.id)
            ]


async def _appended(store: ConversationStore, message: Message, now: datetime) -> None:
    """Append a message as the application does: the document, and the record."""
    await store.append_message(message, message_to_data(message), now=now)


async def _paged(
    store: ConversationStore, owner_id: uuid.UUID, *, limit: int
) -> list[Conversation]:
    """Every conversation of that person, read one page at a time.

    A page at a time is the only way to find out whether the cursor holds: a
    listing read whole says nothing about where one page ends and the next
    begins. It stops itself, so a store that hands back a cursor for ever
    fails here rather than running until the suite is killed.
    """
    seen: list[Conversation] = []
    cursor: str | None = None
    for _ in range(100):
        page = await store.conversations_of(owner_id, limit=limit, cursor=cursor)
        seen.extend(page.conversations)
        cursor = page.cursor
        if cursor is None:
            return seen
    raise AssertionError("the cursor never ran out")
