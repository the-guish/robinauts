# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Watching a run: the stored stream, from a position, until it ends.

What the streaming endpoint is made of (``docs/specs/wire.md``): a person
opens a conversation, is told where to attach (``core.resume_point``), and
then receives the run's events as they are produced. This is the second half
of that, and it promises exactly what ``docs/specs/runs.md`` promises a
watcher:

- **everything after ``after``, in order, nothing twice.** What is yielded is
  read from the store, which is the record, and the position it was stored at
  is what orders it. A watcher that asked from 0 is replayed the run from its
  beginning; one that asked from a later position is not, and the event that
  began the run is never in that slice;
- **it ends where the run ends.** The ``RunEnded`` is yielded and the stream
  is over. A run that had already ended when the watcher attached is replayed
  and ends the same way -- there is no waiting for a run that is not going --
  and so does one that is **no longer there at all**, which is what a
  conversation deleted under a watcher looks like: a run that is gone is over,
  and the stream ends without an error, because nothing about the request was
  wrong and there is nothing left to say;
- **nothing is believed but the store.** The ``RunSignals`` port is how this
  learns that there is something to read without asking the database over and
  over; a signal carries nothing, every wait under it is bounded, and when the
  bound passes the store is read anyway. So a signal that is lost is a slower
  stream and never a watcher that hangs, and the day signals travel through
  ``LISTEN``/``NOTIFY`` nothing here changes;
- **every turn of the loop either sends something or waits.** A signal may
  answer at once -- it remembers what it was told, the end included -- so a
  wake-up that turns out to have nothing behind it must not become a second
  wake-up: the run is looked at again after every wait, which is what ends a
  watcher of a run that is over, and a wake-up that yielded nothing is not
  believed a second time in a row. A watcher that spun would take the loop
  from the run it is watching, which is the worst thing a reader can do to a
  writer;
- **and it does not wait for ever.** A run whose end could not be written, or
  whose process was killed, would otherwise hold a request open until the
  client gave up: after ``quiet_seconds`` with nothing new stored, watching it
  is given up on -- with a line in the log, and by **raising**
  ``domain.RunQuietError``.

**How it ends says which ending it was.** Three of them are ordinary and the
generator simply finishes: the run's ``RunEnded`` was sent; the run is no
longer there; or everything stored after ``after`` was sent and the run had
already ended. A caller that reached the end of the stream therefore knows the
run is over and has, or can read, what became of it. The fourth is the run
that said nothing, and it is the one that raises: "we stopped watching" and
"there is nothing more" are different things to tell somebody, and telling
them apart by what is **missing** from a stream is how a page ends up waiting
for an answer that was never coming (``docs/specs/runs.md``, known limits).

**Ownership is the one rule.** A run is reached through its conversation, and
a conversation of somebody else's is answered exactly like one that does not
exist (``application.owner_of``): "not yours" is "not there", once, before
anything is read.

**It yields the platform's own ``RunEvent``s**, decoded through ``core``'s
stored reader. The wire's shape is ``api``'s -- it maps them to AG-UI -- and
the documents are none of its business; a row this build cannot read is a
fault of ours (``StoredDataError``) and not the watcher's.

**It stops when its reader does.** Nothing is held between two events, so
closing the generator -- which is what a dropped connection comes to -- lets
go of everything at once. A run does not notice: it is not this watcher's, and
whether anybody is watching changes nothing about it.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator

from robinauts.legacy.application.conversations import owner_of
from robinauts.legacy.application.turns import DEFAULT_TURN_SECONDS
from robinauts.legacy.core import run_event_from_stored
from robinauts.legacy.domain import (
    ConversationNotFoundError,
    InvalidValueError,
    Run,
    RunEnded,
    RunEvent,
    RunNotFoundError,
    RunQuietError,
    User,
    chain,
    checked_uuid,
    describe,
)
from robinauts.legacy.ports import ConversationStore, RunSignals

_log = logging.getLogger(__name__)

DEFAULT_WAIT_SECONDS = 5.0
"""How long a watcher waits to be told before it looks anyway.

The bound that turns a lost signal into a poll (``robinauts.legacy.ports.RunSignals``).
Short enough that a stream nobody announced still moves, long enough that a
thousand watchers of quiet runs are not a thousand reads a second. It is never
what the promptness of a live run depends on: that is the announcement, which
arrives the moment the event is stored.

It is never **longer** than the silence a watcher gives up after, and a
``Watch`` built that way is refused: the silence is looked at after each wait,
so a longer wait would give up a whole wait late. A deployment with a short
turn timeout therefore waits in steps of at most that (``robinauts.legacy.app``).
"""

DEFAULT_QUIET_SECONDS = DEFAULT_TURN_SECONDS
"""How long a watcher follows a run that stores nothing before it gives up.

The same bound a turn has, because it is the same question asked from the
other side: a run that has written nothing for as long as a whole turn may
take is a run nothing is writing (``docs/specs/runs.md``, known limits -- a
run whose end could not be written stays active until the next start-up
sweep).

It is a **default and not the rule**: the composition root holds one
``turn_seconds`` and gives it to the run lifecycle and to this, so that a
deployment which lengthens a turn lengthens the wait for one rather than
discovering that the two numbers were copies of each other.
"""


class Watch:
    """A run's events, delivered to whoever may see them, as they are stored."""

    def __init__(
        self,
        *,
        store: ConversationStore,
        signals: RunSignals,
        wait_seconds: float = DEFAULT_WAIT_SECONDS,
        quiet_seconds: float = DEFAULT_QUIET_SECONDS,
    ) -> None:
        self._wait_seconds = _seconds(wait_seconds, "a wait")
        self._quiet_seconds = _seconds(quiet_seconds, "a silence")
        if self._wait_seconds > self._quiet_seconds:
            # The silence is only ever noticed **after** a wait, so a wait
            # longer than it would give up a wait late -- and a deployment
            # that shortened a turn would find that watching one still held a
            # request open for the length of the old bound.
            raise InvalidValueError(
                f"a wait of {self._wait_seconds}s is longer than the silence of"
                f" {self._quiet_seconds}s it gives up after, so it could never give up"
                " in time"
            )
        self._store = store
        self._signals = signals

    async def run(self, user: User, run_id: uuid.UUID) -> Run:
        """That run, if its conversation is this person's; ``RunNotFoundError`` if not.

        The ownership check of ``events``, asked **before** a stream exists.
        A streaming endpoint owes a refusal as a status and a body, and a
        generator can only refuse once bytes are already going out
        (``robinauts.legacy.api.stream_routes``); so whoever is about to open a stream
        asks this first, and is answered 404 -- the same answer for a run that
        is not there and one that is somebody else's -- with nothing sent.

        It also answers **which conversation** the run is of, which the events
        of a re-attached stream do not all carry: a slice that begins after the
        run started holds no ``RunStarted``, and the wire names the
        conversation on every stream.

        ``events`` checks again for itself, because nothing may depend on a
        caller having asked: two reads of one run is what that costs, once per
        stream and never per event.
        """
        checked_uuid(run_id, "a run's id")
        checked_uuid(user.id, "a user's id")
        return await self._owned(user, run_id)

    async def before(self, user: User, run_id: uuid.UUID, *, upto: int) -> tuple[RunEvent, ...]:
        """That run's stored events **up to and including** ``upto``, in order.

        The other half of re-attaching, and the one a stream needs before it
        sends anything: what a watcher receives is everything *after* a
        position, and whoever turns those events into something a client
        renders may have to know what came before them -- a wire whose events
        are bracketed cannot derive the closing bracket of a bracket it never
        saw open (``robinauts.legacy.api.agui``).

        Ownership is checked exactly as ``run`` checks it, because nothing may
        depend on a caller having asked first: a run that is not there and one
        that is somebody else's are the same ``RunNotFoundError``.

        **The bound is the store's**, not a filter over everything it has:
        ``events_of`` takes ``upto`` and reads no row past it, so replaying the
        beginning of a long stream costs the beginning and not the whole of it.

        **What re-attaching costs** is three ownership checks -- the route's,
        this one's and the watcher's -- and two reads of the run's rows. Each
        is once per stream and never per event, and none of them may be left
        out on the grounds that another has been made.

        **And the prefix is the length of the run, in rows and in memory**,
        every time somebody re-attaches: a turn that produced five thousand
        events is five thousand rows read and decoded to answer where its
        thinking stood. Nothing here comes near that -- a re-attach happens
        when a tab is reopened, and a turn is a turn -- and if it ever
        mattered, the read to make is from the **last ``MessageStarted`` at or
        before the position**: everything before that message is about a
        message that is complete, and nothing in it can change what the
        brackets of the one being produced are.
        """
        checked_uuid(run_id, "a run's id")
        checked_uuid(user.id, "a user's id")
        if isinstance(upto, bool) or not isinstance(upto, int) or upto < 0:
            raise InvalidValueError(
                f"a position to read up to is a whole number, not {describe(upto)}"
            )
        await self._owned(user, run_id)
        return tuple(await self._stored(run_id, 0, upto=upto))

    async def events(
        self, user: User, run_id: uuid.UUID, *, after: int = 0
    ) -> AsyncIterator[RunEvent]:
        """That run's events past ``after``, in order, until it has ended.

        ``after`` is the position the watcher last saw: ``0`` asks for the run
        from its beginning, and anything else asks for what followed that
        position -- which is what ``core.resume_point`` hands whoever opened
        the conversation.

        An **async generator**, so nothing at all happens until it is
        iterated, the ownership check and the first read included; and
        whatever holds it releases everything by closing it.

        **It finishes three ways, and raises in a fourth.** It finishes after
        sending the run's ``RunEnded``; after sending what is stored past
        ``after`` of a run that had already ended -- which may be nothing at
        all, when ``after`` is the end of it; and when the run is no longer
        there, its conversation having been deleted under the watcher. In all
        three the run is over and whoever was watching can read what became of
        it. ``RunQuietError`` is the fourth: nothing was stored under a run
        that is **still active** for ``quiet_seconds``, watching it was given
        up on, and that is said rather than left to be guessed from an ending
        that looks like the others.

        ``RunNotFoundError`` for a run that is not there **and** for one in
        somebody else's conversation, which are answered identically
        (``docs/specs/conversations.md``).
        """
        checked_uuid(run_id, "a run's id")
        checked_uuid(user.id, "a user's id")
        if isinstance(after, bool) or not isinstance(after, int) or after < 0:
            raise InvalidValueError(
                f"a position to carry on from is a whole number, not {describe(after)}"
            )
        run: Run | None = await self._owned(user, run_id)
        position = after
        # Whether the last wait came back saying there was something to read,
        # and when something last was: the two things the loop below decides
        # how to wait by.
        told = False
        # Whether this watcher has already said that the signals are failing;
        # see `_waited`.
        complained = False
        silent_since = asyncio.get_running_loop().time()
        while True:
            sent = False
            for event in await self._stored(run_id, position):
                if event.seq <= position:
                    # The store answers in order and past what was asked for;
                    # a watcher never goes back, whatever it is handed.
                    continue
                position, sent = event.seq, True
                yield event
                if isinstance(event.event, RunEnded):
                    return
            now = asyncio.get_running_loop().time()
            if sent:
                silent_since, told = now, False
            else:
                if run is None or not run.is_active:
                    # It has ended, or it is no longer there at all, and what
                    # is stored after `after` has been sent. Nothing follows.
                    return
                if now - silent_since >= self._quiet_seconds:
                    _log.warning(
                        "run %s has stored nothing for %ss and is still %s; watching it"
                        " was given up on",
                        run_id,
                        self._quiet_seconds,
                        run.state.value,
                    )
                    raise RunQuietError(
                        f"run {run_id} stored nothing for {self._quiet_seconds}s"
                        f" and is still {run.state.value}"
                    )
            told, complained = await self._waited(
                run_id, position, told=told, complained=complained
            )
            # **After every wait, look at the run itself.** It is what ends a
            # watcher of a run that finished while it was parked without
            # storing the end it was waiting for, and of one whose rows have
            # gone -- and it is what keeps a wake-up with nothing behind it
            # from being asked for again on the next turn.
            run = await self._store.run_by_id(run_id)

    async def _waited(
        self, run_id: uuid.UUID, position: int, *, told: bool, complained: bool
    ) -> tuple[bool, bool]:
        """Wait for this run to move; whether there is said to be something.

        Ordinarily the signals are waited on, and they may answer at once for
        what was announced before anybody asked -- which is what keeps a
        watcher that was busy writing to a browser from sleeping through what
        arrived meanwhile.

        **A wake-up that led to nothing is not believed twice.** If the last
        wait said there was something and the read that followed sent nothing,
        this one is a real wait of its own, so that a signal out of step with
        the store costs one turn of the loop rather than all of them.

        **And signals that fail are signals that are not there.** A port that
        raised -- a listener whose connection dropped, a process that could
        not reach whatever carries announcements -- would otherwise end a
        stream that the store could have gone on serving perfectly well. It is
        logged and waited out instead, which is exactly what a lost
        announcement costs, and the stream carries on at the rate of the
        bound.

        **Said once per watcher, and quietly after that.** A listener that is
        down stays down: a line every ``wait_seconds`` for every watcher of
        every run would be thousands of copies of one fact, drowning whatever
        else the process has to say. So the first failure a watcher meets is
        the ``WARNING`` -- one line per stream, which is what says how many
        are affected -- and the rest are ``DEBUG``, for whoever is looking.
        ``complained`` is that, carried by the caller because it belongs to
        the stream and not to this service, which is shared by every watcher
        there is.

        Answers **both** of the things the caller carries: whether it was told
        there is something, and whether it has said anything about the
        signals.
        """
        if told:
            await asyncio.sleep(self._wait_seconds)
            return False, complained
        try:
            moved = await self._signals.changed(run_id, position, timeout=self._wait_seconds)
        except asyncio.CancelledError:
            raise
        except Exception as failure:  # noqa: BLE001 - a poll is the fallback
            _log.log(
                logging.DEBUG if complained else logging.WARNING,
                "run %s could not be waited on; watching it falls back to reading: %s",
                run_id,
                chain(failure),
            )
            await asyncio.sleep(self._wait_seconds)
            return False, True
        return moved, complained

    async def _owned(self, user: User, run_id: uuid.UUID) -> Run:
        """That run, if its conversation is this person's; the one rule, once.

        Read **before** anything is streamed, and not again: a run that was
        this person's does not stop being it, and a check repeated on every
        turn of the loop would be a read per event for an answer that cannot
        change.
        """
        run = await self._store.run_by_id(run_id)
        if run is None:
            raise RunNotFoundError(f"there is no run {run_id}")
        try:
            owner_of(
                user,
                run.conversation_id,
                await self._store.conversation_by_id(run.conversation_id),
            )
        except ConversationNotFoundError as missing:
            # The request named a run, so it is told about a run -- and told
            # exactly what it would be told if there were no such run at all.
            raise RunNotFoundError(f"there is no run {run_id}") from missing
        return run

    async def _stored(
        self, run_id: uuid.UUID, position: int, *, upto: int | None = None
    ) -> list[RunEvent]:
        """This run's stored events past ``position``, as records.

        ``upto`` is the other end of the slice, which the store bounds the read
        with rather than this throwing rows away (``ports.ConversationStore``).
        """
        documents = await self._store.events_of(run_id, after=position, upto=upto)
        return [run_event_from_stored(document) for document in documents]


def _seconds(value: object, what: str) -> float:
    """A bound in seconds, or ``InvalidValueError`` saying what was asked for."""
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        raise InvalidValueError(f"{what} is bounded in seconds, not {describe(value)}")
    return float(value)
