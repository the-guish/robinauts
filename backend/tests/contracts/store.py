# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""What every ``Store`` must do: the rules of ``docs/architecture/data-model.md``.

Subclass ``StoreContract`` and override ``new_store``, which returns an empty store, and
``close_store`` when a store has something to release. The documents here are any mapping:
a store keeps them whole and never reads inside.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

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
from robinauts.controller.ports.store import Fence, Store, StoredEvent, StoredMessage
from robinauts.controller.ports.work import HeartbeatResult, Held, WorkQueue

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
MINUTE = timedelta(minutes=1)
EXPIRY = NOW + timedelta(hours=24)
WORKER = "pod-a"
FENCE = Fence(WORKER, 1)


def user(subject: str = "me") -> User:
    return User(uuid.uuid4(), "local", subject, created_at=NOW)


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
    return Turn(
        uuid.uuid4(),
        session_id,
        follows,
        "m",
        TurnState.RUNNING,
        NOW,
        lease_until,
        worker_id=WORKER,
        attempt=1,
        heartbeat_at=NOW,
    )


def piece(position: int, text: str = "x") -> StoredEvent:
    return StoredEvent(
        position, {"v": 1, "kind": "text_piece", "position": position, "text": text}, EXPIRY
    )


def store_test(test: Callable[..., Coroutine[Any, Any, None]]) -> Callable[..., None]:
    """Run the test on a loop of its own, with a fresh store, closed however it ends."""

    def run(self: StoreContract) -> None:
        async def go() -> None:
            store = await self.new_store()
            try:
                await test(self, store)
            finally:
                await self.close_store(store)

        asyncio.run(go())

    # The name and the doc, but not `__wrapped__`: pytest would read the wrapped signature
    # and look for a fixture called `store`.
    run.__name__, run.__qualname__ = test.__name__, test.__qualname__
    run.__doc__, run.__module__ = test.__doc__, test.__module__
    return run


class StoreContract:
    async def new_store(self) -> Store:
        raise NotImplementedError("a StoreContract subclass overrides `new_store`")

    async def close_store(self, store: Store) -> None:
        """Release what the store holds; nothing by default."""

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
        await store.append_events(
            me.id,
            one.id,
            running.id,
            [StoredEvent(event.position, event.document, event.expires_at)],
            at,
            fence=FENCE,
        )

    # --- users --------------------------------------------------------------

    @store_test
    async def test_a_user_is_added_once_and_found_after(self, store: Store) -> None:
        first = User(uuid.uuid4(), "local", "me", name="Me", created_at=NOW)
        again = User(uuid.uuid4(), "local", "me", name="Me again", created_at=NOW)
        assert await store.add_user_if_absent(first) == first
        assert await store.add_user_if_absent(again) == first
        other = User(uuid.uuid4(), "other", "me", created_at=NOW)
        assert await store.add_user_if_absent(other) != first

    # --- sessions -----------------------------------------------------------

    @store_test
    async def test_a_session_is_its_owners_and_not_another_users(self, store: Store) -> None:
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

    @store_test
    async def test_sessions_page_newest_first_and_the_next_page_follows_the_cursor(
        self, store: Store
    ) -> None:
        me = await store.add_user_if_absent(user())
        you = await store.add_user_if_absent(user("you"))
        first = session(me.id, NOW)
        second = session(me.id, NOW + MINUTE)
        third = session(me.id, NOW + 2 * MINUTE)
        for each in (second, third, first):
            await store.add_session(each)
        await store.add_session(session(you.id, NOW + 3 * MINUTE))
        page = await store.sessions_of(me.id, 2, None)
        assert page == [third, second]
        rest = await store.sessions_of(me.id, 2, (page[-1].updated_at, page[-1].id))
        assert rest == [first]
        assert await store.sessions_of(me.id, 2, (first.updated_at, first.id)) == []

    @store_test
    async def test_a_renamed_session_reads_back(self, store: Store) -> None:
        me, one, _, _ = await self.started(store)
        renamed = Session(one.id, me.id, one.agent, one.engine, one.created_at, one.updated_at, "T")
        await store.update_session(renamed)
        assert await store.get_session(me.id, one.id) == renamed

    @store_test
    async def test_a_hide_hides_the_session_whatever_runs_and_its_turn_writes_nothing(
        self, store: Store
    ) -> None:
        me, one, _, running = await self.started(store)
        await store.hide_session(me.id, one.id, NOW)
        with pytest.raises(SessionNotFoundError):
            await store.get_session(me.id, one.id)
        with pytest.raises((TurnLostError, SessionNotFoundError)):
            await self.append(store, me, one, running, piece(1))
        with pytest.raises((TurnLostError, SessionNotFoundError)):
            await store.finish_turn(
                me.id,
                one.id,
                running.id,
                TurnState.CANCELLED,
                NOW,
                None,
                None,
                [],
                NOW,
                fence=FENCE,
            )
        assert isinstance(store, WorkQueue)
        beat = await store.heartbeat(WORKER, [Held(running.id, 1)], NOW, MINUTE)
        assert beat.lost == {running.id}
        on_purged = turn(one.id, uuid.uuid4())
        with pytest.raises(SessionNotFoundError):
            await store.start_turn(me.id, on_purged, None)
        with pytest.raises(SessionNotFoundError):
            await store.hide_session(me.id, one.id, NOW)
        assert await store.sessions_of(me.id, 10, None) == []
        await store.purge_session(me.id, one.id)
        with pytest.raises(SessionNotFoundError):
            await store.get_session(me.id, one.id)
        await store.purge_session(me.id, one.id)

    @store_test
    async def test_a_cancel_is_recorded_once_and_signalled_and_read_back_by_the_heartbeat(
        self, store: Store
    ) -> None:
        assert isinstance(store, WorkQueue)
        me, one, _, running = await self.started(store)
        signals = store.cancel_signals()
        listening = asyncio.ensure_future(anext(signals))
        await asyncio.sleep(0.2)
        cancelled = await store.request_cancel(me.id, one.id, running.id, NOW + MINUTE)
        assert cancelled is not None
        assert cancelled.cancel_requested_at == NOW + MINUTE
        assert await asyncio.wait_for(listening, 5) == running.id
        again = await store.request_cancel(me.id, one.id, running.id, NOW + 2 * MINUTE)
        assert again is not None
        assert again.cancel_requested_at == NOW + MINUTE
        assert await asyncio.wait_for(anext(signals), 5) == running.id
        await signals.aclose()
        beat = await store.heartbeat(WORKER, [Held(running.id, 1)], NOW + MINUTE, MINUTE)
        assert (beat.lost, beat.cancelled) == (frozenset(), {running.id})
        await store.finish_turn(
            me.id, one.id, running.id, TurnState.CANCELLED, NOW, None, None, [], NOW, fence=FENCE
        )
        assert await store.request_cancel(me.id, one.id, running.id, NOW) is None

    @store_test
    async def test_an_expired_turn_whose_cancel_was_asked_for_ends_cancelled(
        self, store: Store
    ) -> None:
        me = await store.add_user_if_absent(user())
        one = session(me.id)
        await store.add_session(one)
        asked = question(one.id)
        running = turn(one.id, asked.id, lease_until=NOW + MINUTE)
        await store.start_turn(me.id, running, asked)
        await store.request_cancel(me.id, one.id, running.id, NOW)
        ended = await store.end_expired_turn(me.id, one.id, running.id, NOW + 2 * MINUTE, None)
        assert ended is not None
        assert (ended.state, ended.error) == (TurnState.CANCELLED, None)

    # --- turns --------------------------------------------------------------

    @store_test
    async def test_a_question_is_stored_with_its_turn_and_a_second_running_turn_refused(
        self, store: Store
    ) -> None:
        me, one, asked, running = await self.started(store)
        assert await store.active_turn(me.id, one.id) == running
        assert await store.latest_turn(me.id, one.id) == running
        assert await store.get_turn(me.id, one.id, running.id) == running
        second = question(one.id, "again")
        while_running = turn(one.id, second.id)
        with pytest.raises(TurnActiveError):
            await store.start_turn(me.id, while_running, second)
        assert await store.messages_of(me.id, one.id) == [asked.document]

    @store_test
    async def test_events_are_numbered_by_the_runner_and_read_after_a_position(
        self, store: Store
    ) -> None:
        me, one, _, running = await self.started(store)
        for n in (1, 2, 3):
            await self.append(store, me, one, running, piece(n, str(n)))
        after_one = await store.events_after(me.id, one.id, running.id, 1)
        assert after_one == [(2, piece(2, "2").document), (3, piece(3, "3").document)]
        await self.append(store, me, one, running, piece(2, "2"))
        other = piece(2, "other")
        with pytest.raises(TurnLostError):
            await self.append(store, me, one, running, other)
        assert len(await store.events_after(me.id, one.id, running.id, 0)) == 3

    @store_test
    async def test_a_finished_turn_keeps_its_answer_and_its_last_events(self, store: Store) -> None:
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
            fence=FENCE,
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

    @store_test
    async def test_an_append_or_a_finish_on_an_ended_turn_is_refused(self, store: Store) -> None:
        me, one, _, running = await self.started(store)
        await store.finish_turn(
            me.id,
            one.id,
            running.id,
            TurnState.FAILED,
            NOW,
            "boom",
            None,
            [piece(1)],
            NOW,
            fence=FENCE,
        )
        too_late = piece(2)
        with pytest.raises(TurnLostError):
            await self.append(store, me, one, running, too_late)
        with pytest.raises(TurnLostError):
            await store.finish_turn(
                me.id, one.id, running.id, TurnState.FINISHED, NOW, None, None, [], NOW, fence=FENCE
            )
        failed = await store.get_turn(me.id, one.id, running.id)
        assert failed is not None
        assert (failed.state, failed.error) == (TurnState.FAILED, "boom")

    @store_test
    async def test_an_append_or_a_finish_past_the_lease_is_refused(self, store: Store) -> None:
        me = await store.add_user_if_absent(user())
        one = session(me.id)
        await store.add_session(one)
        asked = question(one.id)
        running = turn(one.id, asked.id, lease_until=NOW + MINUTE)
        await store.start_turn(me.id, running, asked)
        await self.append(store, me, one, running, piece(1), at=NOW + MINUTE / 2)
        expired = piece(2)
        with pytest.raises(TurnLostError):
            await self.append(store, me, one, running, expired, at=NOW + 2 * MINUTE)
        with pytest.raises(TurnLostError):
            await store.finish_turn(
                me.id,
                one.id,
                running.id,
                TurnState.FINISHED,
                NOW + 2 * MINUTE,
                None,
                None,
                [],
                NOW,
                fence=FENCE,
            )
        assert await store.active_turn(me.id, one.id) == running

    @store_test
    async def test_end_expired_turn_ends_only_a_turn_whose_lease_has_passed(
        self, store: Store
    ) -> None:
        me = await store.add_user_if_absent(user())
        one = session(me.id)
        await store.add_session(one)
        asked = question(one.id)
        running = turn(one.id, asked.id, lease_until=NOW + MINUTE)
        await store.start_turn(me.id, running, asked)
        said = answer(one.id, asked.id, NOW + MINUTE)
        assert (
            await store.end_expired_turn(me.id, one.id, running.id, NOW + MINUTE / 2, None) is None
        )
        assert (
            await store.end_expired_turn(me.id, one.id, uuid.uuid4(), NOW + 2 * MINUTE, None)
            is None
        )
        assert await store.active_turn(me.id, one.id) == running
        ended = await store.end_expired_turn(me.id, one.id, running.id, NOW + 2 * MINUTE, said)
        assert ended is not None
        assert (ended.id, ended.state, ended.ended_at) == (
            running.id,
            TurnState.INTERRUPTED,
            NOW + 2 * MINUTE,
        )
        assert await store.get_turn(me.id, one.id, running.id) == ended
        assert await store.active_turn(me.id, one.id) is None
        assert (
            await store.end_expired_turn(me.id, one.id, running.id, NOW + 3 * MINUTE, None) is None
        )
        assert await store.messages_of(me.id, one.id) == [asked.document, said.document]
        assert (await store.get_session(me.id, one.id)).updated_at == NOW + 2 * MINUTE

    @store_test
    async def test_a_start_and_a_hide_succeed_after_an_expired_turn_is_ended(
        self, store: Store
    ) -> None:
        me = await store.add_user_if_absent(user())
        one = session(me.id)
        await store.add_session(one)
        asked = question(one.id)
        first = turn(one.id, asked.id, lease_until=NOW + MINUTE)
        await store.start_turn(me.id, first, asked)
        assert (
            await store.end_expired_turn(me.id, one.id, first.id, NOW + 2 * MINUTE, None)
            is not None
        )
        again = turn(one.id, asked.id, lease_until=NOW + 5 * MINUTE)
        await store.start_turn(me.id, again, None)
        assert await store.active_turn(me.id, one.id) == again
        assert (
            await store.end_expired_turn(me.id, one.id, again.id, NOW + 6 * MINUTE, None)
            is not None
        )
        await store.hide_session(me.id, one.id, NOW + 6 * MINUTE)
        with pytest.raises(SessionNotFoundError):
            await store.get_session(me.id, one.id)

    @store_test
    async def test_a_turn_of_another_session_is_not_found_through_it(self, store: Store) -> None:
        me, _, _, running = await self.started(store)
        other = session(me.id)
        await store.add_session(other)
        assert await store.get_turn(me.id, other.id, running.id) is None
        assert await store.events_after(me.id, other.id, running.id, 0) == []
        assert await store.active_turn(me.id, other.id) is None
        assert await store.latest_turn(me.id, other.id) is None

    @store_test
    async def test_wait_for_events_returns_on_an_append_and_at_its_timeout(
        self, store: Store
    ) -> None:
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
            me.id,
            one.id,
            running.id,
            TurnState.FINISHED,
            NOW,
            None,
            None,
            [piece(2)],
            NOW,
            fence=FENCE,
        )
        assert await store.wait_for_events(me.id, one.id, running.id, 9, 0.05) is True

    # --- fencing and the heartbeat ------------------------------------------

    @store_test
    async def test_a_write_under_another_fence_is_refused(self, store: Store) -> None:
        me, one, _, running = await self.started(store)
        for fence in (Fence("pod-b", 1), Fence(WORKER, 2)):
            with pytest.raises(TurnLostError):
                await store.append_events(
                    me.id,
                    one.id,
                    running.id,
                    [StoredEvent(1, piece(1).document, EXPIRY)],
                    NOW,
                    fence=fence,
                )
            with pytest.raises(TurnLostError):
                await store.finish_turn(
                    me.id,
                    one.id,
                    running.id,
                    TurnState.FINISHED,
                    NOW,
                    None,
                    None,
                    [],
                    NOW,
                    fence=fence,
                )
        assert await store.events_after(me.id, one.id, running.id, 0) == []
        await self.append(store, me, one, running, piece(1))

    @store_test
    async def test_a_heartbeat_renews_the_lease_of_the_turns_held(self, store: Store) -> None:
        assert isinstance(store, WorkQueue)
        me, one, _, running = await self.started(store)
        later = NOW + 2 * MINUTE
        beat = await store.heartbeat(WORKER, [Held(running.id, 1)], later, 3 * MINUTE)
        assert beat.lost == frozenset()
        renewed = await store.get_turn(me.id, one.id, running.id)
        assert renewed is not None
        assert (renewed.lease_until, renewed.heartbeat_at) == (later + 3 * MINUTE, later)
        # Past the lease it was first written with, the runner still writes.
        await self.append(store, me, one, running, piece(1), at=NOW + 4 * MINUTE)
        assert await store.heartbeat(WORKER, [], later, MINUTE) == HeartbeatResult()

    @store_test
    async def test_a_heartbeat_names_the_turns_this_process_holds_no_more(
        self, store: Store
    ) -> None:
        assert isinstance(store, WorkQueue)
        me, one, _, running = await self.started(store)
        two = session(me.id)
        await store.add_session(two)
        asked = question(two.id)
        ended = turn(two.id, asked.id)
        await store.start_turn(me.id, ended, asked)
        await store.finish_turn(
            me.id, two.id, ended.id, TurnState.FINISHED, NOW, None, None, [], NOW, fence=FENCE
        )
        unknown = uuid.uuid4()
        held = [Held(running.id, 2), Held(ended.id, 1), Held(unknown, 1)]
        beat = await store.heartbeat(WORKER, held, NOW + MINUTE, MINUTE)
        assert beat.lost == {running.id, ended.id, unknown}
        other = await store.heartbeat("pod-b", [Held(running.id, 1)], NOW + MINUTE, MINUTE)
        assert other.lost == {running.id}
        # A lease that has passed is not renewed: the turn is lost, whoever finds it.
        late = await store.heartbeat(WORKER, [Held(running.id, 1)], NOW + 4 * MINUTE, MINUTE)
        assert late.lost == {running.id}
        unchanged = await store.get_turn(me.id, one.id, running.id)
        assert unchanged == running

    # --- batches ----------------------------------------------------------------

    @store_test
    async def test_a_batch_is_appended_whole_and_its_last_position_read_without_events(
        self, store: Store
    ) -> None:
        me, one, _, running = await self.started(store)
        assert await store.last_position(me.id, one.id, running.id) == 0
        batch = [piece(1, "a"), piece(2, "b"), piece(3, "c")]
        await store.append_events(me.id, one.id, running.id, batch, NOW, fence=FENCE)
        assert await store.last_position(me.id, one.id, running.id) == 3
        # The same batch again is the runner's own, acknowledged late.
        await store.append_events(me.id, one.id, running.id, batch, NOW, fence=FENCE)
        clash = [piece(3, "c"), piece(4, "d")]
        with pytest.raises(TurnLostError):
            await store.append_events(me.id, one.id, running.id, clash, NOW, fence=FENCE)
        assert len(await store.events_after(me.id, one.id, running.id, 0)) == 3
