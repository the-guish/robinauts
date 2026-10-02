# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""What every ``Store`` must do: the rules of ``docs/architecture/data-model.md``.

Subclass ``StoreContract`` and override ``new_store``, which returns an empty store. The
documents here are any mapping: a store keeps them whole and never reads inside.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from aio import asyncio_test
from robinauts.controller.contract.domain import (
    Role,
    Session,
    SessionNotFoundError,
    Turn,
    TurnActiveError,
    TurnLostError,
    TurnState,
    User,
)
from robinauts.controller.ports.store import Store, StoredEvent, StoredMessage

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
MINUTE = timedelta(minutes=1)
EXPIRY = NOW + timedelta(hours=24)


def user(subject: str = "me") -> User:
    return User(uuid.uuid4(), "local", subject)


def session(owner: uuid.UUID, updated_at: datetime = NOW) -> Session:
    return Session(uuid.uuid4(), owner, "a", "echo", NOW, updated_at)


def question(session_id: uuid.UUID, text: str = "hi") -> StoredMessage:
    return StoredMessage(uuid.uuid4(), session_id, None, Role.USER, NOW, {"v": 1, "text": text})


def answer(session_id: uuid.UUID, parent: uuid.UUID, at: datetime = NOW) -> StoredMessage:
    return StoredMessage(
        uuid.uuid4(), session_id, parent, Role.ASSISTANT, at, {"v": 1, "text": "hello"}
    )


def turn(
    session_id: uuid.UUID, follows: uuid.UUID, lease_until: datetime = NOW + 3 * MINUTE
) -> Turn:
    return Turn(uuid.uuid4(), session_id, follows, "m", TurnState.RUNNING, NOW, lease_until)


def piece(position: int, text: str = "x") -> StoredEvent:
    return StoredEvent(
        position, {"v": 1, "kind": "text_piece", "position": position, "text": text}, EXPIRY
    )


class StoreContract:
    async def new_store(self) -> Store:
        raise NotImplementedError("a StoreContract subclass overrides `new_store`")

    async def started(self, store: Store) -> tuple[User, Session, StoredMessage, Turn]:
        """A session with its first question and a running turn."""
        me = user()
        await store.add_user_if_absent(me)
        one = session(me.id)
        await store.add_session(one)
        asked = question(one.id)
        running = turn(one.id, asked.id)
        await store.start_turn(me.id, running, asked)
        return me, one, asked, running

    async def append(
        self,
        store: Store,
        me: User,
        one: Session,
        running: Turn,
        event: StoredEvent,
        at: datetime = NOW,
    ) -> None:
        await store.append_event(
            me.id, one.id, running.id, event.position, event.document, at, event.expires_at
        )

    # --- users --------------------------------------------------------------

    @asyncio_test
    async def test_a_user_is_added_once_and_found_after(self) -> None:
        store = await self.new_store()
        first = User(uuid.uuid4(), "local", "me", name="Me")
        again = User(uuid.uuid4(), "local", "me", name="Me again")
        assert await store.add_user_if_absent(first) == first
        assert await store.add_user_if_absent(again) == first
        assert await store.add_user_if_absent(User(uuid.uuid4(), "other", "me")) != first

    # --- sessions -----------------------------------------------------------

    @asyncio_test
    async def test_a_session_is_its_owners_and_not_another_users(self) -> None:
        store = await self.new_store()
        me, one, asked, _ = await self.started(store)
        assert await store.get_session(me.id, one.id) == one
        assert await store.messages_of(me.id, one.id) == [asked.document]
        other = user("you")
        with pytest.raises(SessionNotFoundError):
            await store.get_session(other.id, one.id)
        with pytest.raises(SessionNotFoundError):
            await store.messages_of(other.id, one.id)
        with pytest.raises(SessionNotFoundError):
            await store.get_session(me.id, uuid.uuid4())

    @asyncio_test
    async def test_sessions_page_newest_first_and_the_next_page_follows_the_cursor(self) -> None:
        store = await self.new_store()
        me = user()
        first = session(me.id, NOW)
        second = session(me.id, NOW + MINUTE)
        third = session(me.id, NOW + 2 * MINUTE)
        for each in (second, third, first):
            await store.add_session(each)
        await store.add_session(session(user("you").id, NOW + 3 * MINUTE))
        page = await store.sessions_of(me.id, 2, None)
        assert page == [third, second]
        rest = await store.sessions_of(me.id, 2, (page[-1].updated_at, page[-1].id))
        assert rest == [first]
        assert await store.sessions_of(me.id, 2, (first.updated_at, first.id)) == []

    @asyncio_test
    async def test_a_renamed_session_reads_back(self) -> None:
        store = await self.new_store()
        me, one, _, _ = await self.started(store)
        renamed = Session(one.id, me.id, one.agent, one.engine, one.created_at, one.updated_at, "T")
        await store.update_session(renamed)
        assert await store.get_session(me.id, one.id) == renamed

    @asyncio_test
    async def test_a_hide_is_refused_while_a_turn_runs_and_hides_the_session_after(self) -> None:
        store = await self.new_store()
        me, one, _, running = await self.started(store)
        with pytest.raises(TurnActiveError):
            await store.hide_session(me.id, one.id, NOW)
        await store.finish_turn(
            me.id, one.id, running.id, TurnState.CANCELLED, NOW, None, None, [piece(1)], NOW
        )
        await store.hide_session(me.id, one.id, NOW)
        with pytest.raises(SessionNotFoundError):
            await store.get_session(me.id, one.id)
        with pytest.raises(SessionNotFoundError):
            await store.start_turn(me.id, turn(one.id, uuid.uuid4()), None)
        assert await store.sessions_of(me.id, 10, None) == []
        await store.purge_session(me.id, one.id)
        with pytest.raises(SessionNotFoundError):
            await store.get_session(me.id, one.id)
        await store.purge_session(me.id, one.id)

    # --- turns --------------------------------------------------------------

    @asyncio_test
    async def test_a_question_is_stored_with_its_turn_and_a_second_running_turn_refused(
        self,
    ) -> None:
        store = await self.new_store()
        me, one, asked, running = await self.started(store)
        assert await store.active_turn(me.id, one.id) == running
        assert await store.latest_turn(me.id, one.id) == running
        assert await store.get_turn(me.id, one.id, running.id) == running
        second = question(one.id, "again")
        with pytest.raises(TurnActiveError):
            await store.start_turn(me.id, turn(one.id, second.id), second)
        assert await store.messages_of(me.id, one.id) == [asked.document]

    @asyncio_test
    async def test_events_are_numbered_by_the_runner_and_read_after_a_position(self) -> None:
        store = await self.new_store()
        me, one, _, running = await self.started(store)
        for n in (1, 2, 3):
            await self.append(store, me, one, running, piece(n, str(n)))
        after_one = await store.events_after(me.id, one.id, running.id, 1)
        assert after_one == [(2, piece(2, "2").document), (3, piece(3, "3").document)]
        await self.append(store, me, one, running, piece(2, "2"))
        with pytest.raises(TurnLostError):
            await self.append(store, me, one, running, piece(2, "other"))
        assert len(await store.events_after(me.id, one.id, running.id, 0)) == 3

    @asyncio_test
    async def test_a_finished_turn_keeps_its_answer_and_its_last_events(self) -> None:
        store = await self.new_store()
        me, one, asked, running = await self.started(store)
        await self.append(store, me, one, running, piece(1))
        said = answer(one.id, asked.id, NOW + MINUTE)
        later = NOW + 2 * MINUTE
        await store.finish_turn(
            me.id,
            one.id,
            running.id,
            TurnState.FINISHED,
            later,
            None,
            said,
            [piece(2, "completed"), piece(3, "ended")],
            later,
        )
        assert await store.messages_of(me.id, one.id) == [asked.document, said.document]
        positions = [p for p, _ in await store.events_after(me.id, one.id, running.id, 0)]
        assert positions == [1, 2, 3]
        finished = await store.get_turn(me.id, one.id, running.id)
        assert finished is not None
        assert (finished.state, finished.ended_at, finished.error) == (
            TurnState.FINISHED,
            later,
            None,
        )
        assert (await store.get_session(me.id, one.id)).updated_at == later
        assert await store.active_turn(me.id, one.id) is None
        assert await store.latest_turn(me.id, one.id) == finished

    @asyncio_test
    async def test_an_append_or_a_finish_on_an_ended_turn_is_refused(self) -> None:
        store = await self.new_store()
        me, one, _, running = await self.started(store)
        await store.finish_turn(
            me.id, one.id, running.id, TurnState.FAILED, NOW, "boom", None, [piece(1)], NOW
        )
        with pytest.raises(TurnLostError):
            await self.append(store, me, one, running, piece(2))
        with pytest.raises(TurnLostError):
            await store.finish_turn(
                me.id, one.id, running.id, TurnState.FINISHED, NOW, None, None, [], NOW
            )
        failed = await store.get_turn(me.id, one.id, running.id)
        assert failed is not None
        assert (failed.state, failed.error) == (TurnState.FAILED, "boom")

    @asyncio_test
    async def test_an_append_or_a_finish_past_the_lease_is_refused(self) -> None:
        store = await self.new_store()
        me = user()
        one = session(me.id)
        await store.add_session(one)
        asked = question(one.id)
        running = turn(one.id, asked.id, lease_until=NOW + MINUTE)
        await store.start_turn(me.id, running, asked)
        await self.append(store, me, one, running, piece(1), at=NOW + MINUTE / 2)
        with pytest.raises(TurnLostError):
            await self.append(store, me, one, running, piece(2), at=NOW + 2 * MINUTE)
        with pytest.raises(TurnLostError):
            await store.finish_turn(
                me.id, one.id, running.id, TurnState.FINISHED, NOW + 2 * MINUTE, None, None, [], NOW
            )
        assert await store.active_turn(me.id, one.id) == running

    @asyncio_test
    async def test_end_expired_turn_ends_only_a_turn_whose_lease_has_passed(self) -> None:
        store = await self.new_store()
        me = user()
        one = session(me.id)
        await store.add_session(one)
        asked = question(one.id)
        running = turn(one.id, asked.id, lease_until=NOW + MINUTE)
        await store.start_turn(me.id, running, asked)
        assert await store.end_expired_turn(me.id, one.id, NOW + MINUTE / 2) is None
        assert await store.active_turn(me.id, one.id) == running
        ended = await store.end_expired_turn(me.id, one.id, NOW + 2 * MINUTE)
        assert ended is not None
        assert (ended.id, ended.state, ended.ended_at) == (
            running.id,
            TurnState.INTERRUPTED,
            NOW + 2 * MINUTE,
        )
        assert await store.get_turn(me.id, one.id, running.id) == ended
        assert await store.active_turn(me.id, one.id) is None
        assert await store.end_expired_turn(me.id, one.id, NOW + 3 * MINUTE) is None

    @asyncio_test
    async def test_a_start_and_a_hide_succeed_after_an_expired_turn_is_ended(self) -> None:
        store = await self.new_store()
        me = user()
        one = session(me.id)
        await store.add_session(one)
        asked = question(one.id)
        await store.start_turn(me.id, turn(one.id, asked.id, lease_until=NOW + MINUTE), asked)
        assert await store.end_expired_turn(me.id, one.id, NOW + 2 * MINUTE) is not None
        again = turn(one.id, asked.id, lease_until=NOW + 5 * MINUTE)
        await store.start_turn(me.id, again, None)
        assert await store.active_turn(me.id, one.id) == again
        assert await store.end_expired_turn(me.id, one.id, NOW + 6 * MINUTE) is not None
        await store.hide_session(me.id, one.id, NOW + 6 * MINUTE)
        with pytest.raises(SessionNotFoundError):
            await store.get_session(me.id, one.id)

    @asyncio_test
    async def test_a_turn_of_another_session_is_not_found_through_it(self) -> None:
        store = await self.new_store()
        me, _, _, running = await self.started(store)
        other = session(me.id)
        await store.add_session(other)
        assert await store.get_turn(me.id, other.id, running.id) is None
        assert await store.events_after(me.id, other.id, running.id, 0) == []
        assert await store.active_turn(me.id, other.id) is None
        assert await store.latest_turn(me.id, other.id) is None

    @asyncio_test
    async def test_wait_for_events_returns_on_an_append_and_at_its_timeout(self) -> None:
        store = await self.new_store()
        me, one, _, running = await self.started(store)
        assert await store.wait_for_events(me.id, one.id, running.id, 0, 0.05) is False

        async def soon() -> None:
            await asyncio.sleep(0.02)
            await self.append(store, me, one, running, piece(1))

        appending = asyncio.create_task(soon())
        assert await store.wait_for_events(me.id, one.id, running.id, 0, 5.0) is True
        await appending
        assert await store.wait_for_events(me.id, one.id, running.id, 1, 0.05) is False
        await store.finish_turn(
            me.id, one.id, running.id, TurnState.FINISHED, NOW, None, None, [piece(2)], NOW
        )
        assert await store.wait_for_events(me.id, one.id, running.id, 9, 0.05) is True
