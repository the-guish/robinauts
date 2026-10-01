# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""What every ``CredentialStore`` must do: users, sessions, sign-ins, expiry.

Subclass ``CredentialStoreContract`` and override ``new_store`` to return an
empty store. It is a coroutine, and it is awaited inside the test's own event
loop, so a store that has to open connections may do so there: a connection
made on another loop would be unusable on this one.

The in-memory fake passes this now; the PostgreSQL store is held to the same
suite when it lands, which is the point of writing it here rather than beside
the fake.

Two kinds of promise are checked. The plain ones -- what is stored, what is
found, what has expired -- and the ones about **two things happening at
once**: a sign-in taken by one callback of two, a cap that holds when the
sign-ins arrive together, one user for one person signing in twice, one sign-
out of two. Those are what a store built out of a read and a later write
quietly breaks, and a suite without them certifies such a store as correct.
``tests/unit/test_fake_credential_store.py`` keeps them honest by running them
against a store that is deliberately racy and requiring them to fail.

No test sleeps for an expiry. Every expiry is a datetime the test chose and
handed in, so a store that reads a clock of its own fails these, as the port
says it must.

What this asks of an implementation:

- **it is called concurrently**, and must stay correct when it is. One
  connection shared by every caller is not enough: the tests below run
  several calls at once, as a web server does. A pool, or a connection per
  call.
- **the cap holds exactly**. Counting the sign-ins in progress and inserting
  one must be indivisible. In PostgreSQL that is not what a ``SELECT count``
  followed by an ``INSERT`` gives, whatever the isolation level, because the
  rows being counted are rows the other transaction has not committed yet:
  it takes a transaction-scoped advisory lock on the table, or
  ``SERIALIZABLE`` with a retry on serialization failure.
- **taking a sign-in is one statement.** ``DELETE ... WHERE state_hash = $1
  RETURNING ...``, not a ``SELECT`` and then a ``DELETE``.
- **a user is got or created in one step**, ``INSERT ... ON CONFLICT
  (provider, subject) DO UPDATE ... RETURNING``, so that two sign-ins of one
  person leave one row.
- **sweeping runs beside everything else.** A delete of expired rows may not
  make a concurrent take or lookup fail.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest

from aio import asyncio_test
from robinauts.legacy.core import secret_hash
from robinauts.legacy.domain import InvalidValueError, PendingLogin
from robinauts.legacy.ports import CredentialStore

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
LATER = NOW + timedelta(hours=1)
MUCH_LATER = NOW + timedelta(days=1)


NONCE = "the-nonce-" + "n" * 33
VERIFIER = "the-verifier-" + "v" * 51
"""Recognisable, and the length a real one has, so that a dump can be searched."""


def pending(provider: str = "google", **changes: object) -> PendingLogin:
    """A sign-in in progress, expiring an hour from ``NOW`` unless told otherwise."""
    fields: dict[str, object] = {
        "provider": provider,
        "nonce": NONCE,
        "verifier": VERIFIER,
        "created_at": NOW,
        "expires_at": LATER,
    }
    fields.update(changes)
    return PendingLogin(**fields)  # type: ignore[arg-type]


class CredentialStoreContract:
    """Subclass this and override ``new_store`` to return an empty store."""

    async def new_store(self) -> CredentialStore:
        """An empty store, built inside the event loop the test runs on."""
        raise NotImplementedError("a CredentialStoreContract subclass overrides `new_store`")

    async def close_store(self, store: CredentialStore | None) -> None:
        """Let go of whatever ``new_store`` took. Nothing, unless it took something.

        ``None`` when ``new_store`` did not finish. An implementation that
        takes something -- a pool, a schema, a file -- may already have taken
        half of it by then, so this is called for that case too and has to
        cope with it. Leaking after a failure is how one broken test turns
        into a run that fails in a different place every time.
        """

    @asynccontextmanager
    async def opened(self) -> AsyncIterator[CredentialStore]:
        """An empty store for one test, closed however the test ends.

        Every test below goes through this. A store that holds a connection
        pool opens it in ``new_store`` and closes it here, on the loop that
        made it -- each test has a loop of its own, and a pool outliving its
        loop is a warning at best and a hang at worst.

        ``new_store`` is inside the ``try``, so a store that fails half way
        through being built is still handed to ``close_store``.
        """
        store: CredentialStore | None = None
        try:
            store = await self.new_store()
            yield store
        finally:
            await self.close_store(store)

    async def dump(self, store: CredentialStore) -> str:
        """Everything the store holds, as text: every row, **every field**.

        There is no way through the port itself to ask a store what it keeps,
        and "it keeps no secret" is the one promise worth checking from
        outside. A subclass says how: the in-memory one writes out each field
        of each record it holds, a database one selects every column of every
        row of every table of its own. Not a ``repr`` that leaves fields out:
        what is left out is exactly where a secret would be found.
        """
        raise NotImplementedError("a CredentialStoreContract subclass overrides `dump`")

    @asyncio_test
    async def test_the_two_secrets_a_browser_carries_are_kept_as_hashes(self) -> None:
        # The session cookie and the ``state``: those two, and only those two,
        # are what the store is asked about and never told. The nonce and the
        # PKCE verifier of a sign-in **are** in the row, by design -- the
        # callback compares them with what the provider sent, and a hash
        # cannot be compared with something it has not seen.
        async with self.opened() as store:
            user = await store.user_at_sign_in("google", "1", name=None, email=None, now=NOW)
            await store.add_session(
                secret_hash("the-cookie"), user.id, created_at=NOW, expires_at=MUCH_LATER
            )
            await store.add_pending_login(secret_hash("the-state"), pending(), limit=10, now=NOW)

            held = await self.dump(store)

            assert "the-cookie" not in held
            assert "the-state" not in held
            assert secret_hash("the-cookie") in held
            assert secret_hash("the-state") in held
            # The dump is worth what it shows: a row's own fields are in it.
            assert NONCE in held
            assert VERIFIER in held

    # Users.

    @asyncio_test
    async def test_a_user_is_created_at_their_first_sign_in(self) -> None:
        async with self.opened() as store:
            user = await store.user_at_sign_in(
                "google", "248289761001", name="Ada Lovelace", email="ada@example.com", now=NOW
            )

            assert (user.provider, user.subject) == ("google", "248289761001")
            assert (user.name, user.email) == ("Ada Lovelace", "ada@example.com")
            assert user.created_at == NOW
            assert await store.user_by_id(user.id) == user

    @asyncio_test
    async def test_the_second_sign_in_finds_the_user_and_refreshes_what_changed(self) -> None:
        async with self.opened() as store:
            first = await store.user_at_sign_in(
                "google", "1", name="Ada", email="ada@example.com", now=NOW
            )

            again = await store.user_at_sign_in(
                "google", "1", name="Ada Lovelace", email="ada@example.org", now=MUCH_LATER
            )

            assert (again.id, again.created_at) == (first.id, first.created_at)
            assert (again.name, again.email) == ("Ada Lovelace", "ada@example.org")
            assert await store.user_by_id(first.id) == again

    @asyncio_test
    async def test_a_sign_in_without_a_verified_address_clears_the_one_kept(self) -> None:
        async with self.opened() as store:
            first = await store.user_at_sign_in(
                "okta", "1", name="Ada", email="ada@example.com", now=NOW
            )

            again = await store.user_at_sign_in("okta", "1", name="Ada", email=None, now=LATER)

            assert again.id == first.id
            assert again.email is None

    @asyncio_test
    async def test_one_subject_at_two_providers_is_two_users(self) -> None:
        async with self.opened() as store:
            google = await store.user_at_sign_in("google", "1", name=None, email=None, now=NOW)
            okta = await store.user_at_sign_in("okta", "1", name=None, email=None, now=NOW)

            assert google.id != okta.id
            assert await store.user_by_id(google.id) == google
            assert await store.user_by_id(okta.id) == okta

    @asyncio_test
    async def test_an_unknown_user_id_is_nobody(self) -> None:
        async with self.opened() as store:
            assert await store.user_by_id(uuid.uuid4()) is None

    @asyncio_test
    async def test_a_user_is_found_by_their_key_without_being_written(self) -> None:
        """Asking who somebody is must not be a write (``ports.user_by_key``).

        ``user_at_sign_in`` rewrites the row every time, which is what a
        sign-in wants and what a caller asking on every request must not pay
        for. So: the same user comes back, an unknown key is nobody, and
        asking about one changes nothing in the store.
        """
        async with self.opened() as store:
            user = await store.user_at_sign_in(
                "google", "1", name="Ada", email="ada@example.com", now=NOW
            )
            before = await self.dump(store)

            assert await store.user_by_key("google", "1") == user
            # The key is both halves: the same subject elsewhere is nobody.
            assert await store.user_by_key("okta", "1") is None
            assert await store.user_by_key("google", "2") is None
            assert await self.dump(store) == before

    @asyncio_test
    async def test_asking_about_a_user_who_is_not_there_creates_nobody(self) -> None:
        async with self.opened() as store:
            empty = await self.dump(store)

            assert await store.user_by_key("google", "1") is None

            assert await self.dump(store) == empty

    # Sessions.

    @asyncio_test
    async def test_a_session_is_found_by_the_hash_of_its_secret(self) -> None:
        async with self.opened() as store:
            user = await store.user_at_sign_in("google", "1", name=None, email=None, now=NOW)

            session = await store.add_session(
                secret_hash("s3cret"), user.id, created_at=NOW, expires_at=MUCH_LATER
            )

            assert (session.user_id, session.created_at, session.expires_at) == (
                user.id,
                NOW,
                MUCH_LATER,
            )
            assert await store.session_by_hash(secret_hash("s3cret"), now=LATER) == session

    @asyncio_test
    async def test_a_session_is_not_found_by_its_secret(self) -> None:
        async with self.opened() as store:
            user = await store.user_at_sign_in("google", "1", name=None, email=None, now=NOW)
            await store.add_session(
                secret_hash("s3cret"), user.id, created_at=NOW, expires_at=MUCH_LATER
            )

            assert await store.session_by_hash("s3cret", now=NOW) is None
            assert await store.session_by_hash(secret_hash("s3crey"), now=NOW) is None

    @asyncio_test
    async def test_an_expired_session_is_as_good_as_absent(self) -> None:
        async with self.opened() as store:
            user = await store.user_at_sign_in("google", "1", name=None, email=None, now=NOW)
            await store.add_session(secret_hash("s"), user.id, created_at=NOW, expires_at=LATER)

            assert await store.session_by_hash(secret_hash("s"), now=LATER) is None
            assert await store.session_by_hash(secret_hash("s"), now=MUCH_LATER) is None

    @asyncio_test
    async def test_a_deleted_session_is_gone_and_deleting_it_twice_says_so(self) -> None:
        async with self.opened() as store:
            user = await store.user_at_sign_in("google", "1", name=None, email=None, now=NOW)
            await store.add_session(
                secret_hash("s"), user.id, created_at=NOW, expires_at=MUCH_LATER
            )

            assert await store.delete_session(secret_hash("s")) is True
            assert await store.delete_session(secret_hash("s")) is False
            assert await store.session_by_hash(secret_hash("s"), now=NOW) is None

    @asyncio_test
    async def test_sweeping_sessions_deletes_the_expired_ones_alone(self) -> None:
        async with self.opened() as store:
            user = await store.user_at_sign_in("google", "1", name=None, email=None, now=NOW)
            await store.add_session(secret_hash("short"), user.id, created_at=NOW, expires_at=LATER)
            await store.add_session(
                secret_hash("long"), user.id, created_at=NOW, expires_at=MUCH_LATER
            )

            assert await store.delete_expired_sessions(now=NOW) == 0
            assert await store.delete_expired_sessions(now=LATER) == 1
            assert await store.delete_expired_sessions(now=LATER) == 0
            assert await store.session_by_hash(secret_hash("long"), now=LATER) is not None

    # Sign-ins in progress.

    @asyncio_test
    async def test_a_sign_in_is_taken_once(self) -> None:
        async with self.opened() as store:
            login = pending()
            assert await store.add_pending_login(secret_hash("state"), login, limit=10, now=NOW)

            assert await store.take_pending_login(secret_hash("state")) == login
            assert await store.take_pending_login(secret_hash("state")) is None

    @asyncio_test
    async def test_a_sign_in_is_not_found_by_its_state(self) -> None:
        async with self.opened() as store:
            await store.add_pending_login(secret_hash("state"), pending(), limit=10, now=NOW)

            assert await store.take_pending_login("state") is None
            assert await store.take_pending_login(secret_hash("statf")) is None

    @asyncio_test
    async def test_an_expired_sign_in_is_handed_back_for_the_caller_to_refuse(self) -> None:
        async with self.opened() as store:
            # Expiry is one decision, made by the application: the store returns
            # the row, and removes it either way.
            login = pending(expires_at=LATER)
            await store.add_pending_login(secret_hash("state"), login, limit=10, now=NOW)

            assert await store.take_pending_login(secret_hash("state")) == login
            assert await store.take_pending_login(secret_hash("state")) is None

    @asyncio_test
    async def test_pending_sign_ins_are_counted_without_the_expired_ones(self) -> None:
        async with self.opened() as store:
            await store.add_pending_login(secret_hash("a"), pending(), limit=10, now=NOW)
            await store.add_pending_login(
                secret_hash("b"), pending(expires_at=MUCH_LATER), limit=10, now=NOW
            )

            assert await store.count_pending_logins(now=NOW) == 2
            assert await store.count_pending_logins(now=LATER) == 1
            assert await store.count_pending_logins(now=MUCH_LATER) == 0

    @asyncio_test
    async def test_the_cap_refuses_a_sign_in_and_stores_nothing(self) -> None:
        async with self.opened() as store:
            assert await store.add_pending_login(secret_hash("a"), pending(), limit=2, now=NOW)
            assert await store.add_pending_login(secret_hash("b"), pending(), limit=2, now=NOW)

            assert (
                await store.add_pending_login(secret_hash("c"), pending(), limit=2, now=NOW)
                is False
            )
            assert await store.count_pending_logins(now=NOW) == 2
            assert await store.take_pending_login(secret_hash("c")) is None

    @asyncio_test
    async def test_an_expired_sign_in_does_not_hold_a_place_under_the_cap(self) -> None:
        async with self.opened() as store:
            await store.add_pending_login(secret_hash("old"), pending(), limit=1, now=NOW)

            assert await store.add_pending_login(
                secret_hash("new"), pending(), limit=1, now=NOW
            ) is (False)
            assert await store.add_pending_login(
                secret_hash("new"), pending(expires_at=MUCH_LATER), limit=1, now=LATER
            )

    @asyncio_test
    async def test_a_hash_in_use_is_refused_and_the_sign_in_under_it_kept(self) -> None:
        async with self.opened() as store:
            # It cannot happen -- a state carries 256 random bits. What a store
            # does about it is settled all the same, and it is not to end the
            # sign-in somebody is in the middle of.
            await store.add_pending_login(
                secret_hash("state"), pending("google"), limit=10, now=NOW
            )

            again = await store.add_pending_login(
                secret_hash("state"), pending("okta"), limit=10, now=NOW
            )

            assert again is False
            assert await store.count_pending_logins(now=NOW) == 1
            taken = await store.take_pending_login(secret_hash("state"))
            assert taken is not None
            assert taken.provider == "google"

    @asyncio_test
    async def test_a_hash_in_use_is_refused_at_the_cap_too(self) -> None:
        async with self.opened() as store:
            await store.add_pending_login(secret_hash("state"), pending("google"), limit=1, now=NOW)

            assert (
                await store.add_pending_login(
                    secret_hash("state"), pending("okta"), limit=1, now=NOW
                )
                is False
            )
            taken = await store.take_pending_login(secret_hash("state"))
            assert taken is not None
            assert taken.provider == "google"

    @asyncio_test
    async def test_sweeping_sign_ins_deletes_the_expired_ones_alone(self) -> None:
        async with self.opened() as store:
            await store.add_pending_login(secret_hash("short"), pending(), limit=10, now=NOW)
            await store.add_pending_login(
                secret_hash("long"), pending(expires_at=MUCH_LATER), limit=10, now=NOW
            )

            assert await store.delete_expired_pending_logins(now=NOW) == 0
            assert await store.delete_expired_pending_logins(now=LATER) == 1
            assert await store.delete_expired_pending_logins(now=LATER) == 0
            assert await store.take_pending_login(secret_hash("long")) is not None

    @asyncio_test
    async def test_a_sign_in_keeps_everything_its_callback_needs(self) -> None:
        async with self.opened() as store:
            login = pending("okta", return_to="/#/chat/7")
            await store.add_pending_login(secret_hash("state"), login, limit=10, now=NOW)

            taken = await store.take_pending_login(secret_hash("state"))

            assert taken is not None
            assert (taken.provider, taken.nonce, taken.verifier) == (
                login.provider,
                login.nonce,
                login.verifier,
            )
            assert (taken.created_at, taken.expires_at) == (NOW, LATER)
            assert taken.return_to == "/#/chat/7"

    # What a store refuses.
    #
    # Four things the port leaves unsaid, which two stores answered
    # differently -- and two answers is the same as no decision. They are
    # decided here, all four the same way: a store refuses, with a domain
    # error, rather than doing something it cannot take back. The refusal is
    # about *storing*; looking something impossible up stays "not found",
    # because a lookup is what an attacker controls and it must not tell
    # them apart from a miss.

    @asyncio_test
    async def test_a_secret_hash_a_session_already_holds_is_refused(self) -> None:
        # It cannot happen -- a session secret carries 256 random bits --
        # and what a store does about it is settled all the same. The same
        # answer as a pending sign-in's: overwriting would end the session
        # somebody is using, to make room for a collision that did not
        # happen.
        async with self.opened() as store:
            user = await store.user_at_sign_in("google", "1", name=None, email=None, now=NOW)
            first = await store.add_session(
                secret_hash("s"), user.id, created_at=NOW, expires_at=MUCH_LATER
            )

            with pytest.raises(InvalidValueError):
                await store.add_session(
                    secret_hash("s"), user.id, created_at=LATER, expires_at=MUCH_LATER
                )

            assert await store.session_by_hash(secret_hash("s"), now=NOW) == first

    @asyncio_test
    async def test_a_session_for_somebody_who_is_not_a_user_is_refused(self) -> None:
        # A session names its user, and the routes that follow it will look
        # that user up to decide what may be seen. One pointing at nobody is
        # a row no code has an answer for, so it is never written.
        async with self.opened() as store:
            with pytest.raises(InvalidValueError):
                await store.add_session(
                    secret_hash("s"), uuid.uuid4(), created_at=NOW, expires_at=MUCH_LATER
                )

    @asyncio_test
    async def test_a_key_that_is_not_the_hash_of_a_secret_is_refused(self) -> None:
        # The port is handed the SHA-256 of a secret and never the secret
        # itself. A store that wrote down whatever it was given would, the
        # day a caller passed the wrong one, keep the secret in the clear --
        # which is the one thing the whole arrangement exists to prevent. So
        # a key that is not 64 hexadecimal digits is refused on the way in.
        async with self.opened() as store:
            user = await store.user_at_sign_in("google", "1", name=None, email=None, now=NOW)

            with pytest.raises(InvalidValueError):
                await store.add_session("s3cret", user.id, created_at=NOW, expires_at=MUCH_LATER)
            with pytest.raises(InvalidValueError):
                await store.add_pending_login("the-state", pending(), limit=10, now=NOW)

            # Asking about one is not refused; it is simply not found.
            assert await store.session_by_hash("s3cret", now=NOW) is None
            assert await store.take_pending_login("the-state") is None

    @asyncio_test
    async def test_a_key_that_is_not_a_hash_is_refused_at_the_cap_too(self) -> None:
        # The reason the check cannot be left to the storage. At the cap
        # nothing is stored, so a store that found out only by trying would
        # answer ``False`` -- "busy, try again" -- about a key that was never
        # a key, and the caller would go on believing it had handed over a
        # hash. What is wrong with the call does not depend on how full the
        # table is.
        async with self.opened() as store:
            assert await store.add_pending_login(secret_hash("a"), pending(), limit=1, now=NOW)

            with pytest.raises(InvalidValueError):
                await store.add_pending_login("the-state", pending(), limit=1, now=NOW)

            assert await store.count_pending_logins(now=NOW) == 1

    @asyncio_test
    async def test_a_time_that_names_no_instant_is_refused_by_every_method(self) -> None:
        # The store is told what "now" is, by the one clock. A naive datetime
        # names no instant: read as UTC by one deployment and as local time by
        # the next, it would quietly lengthen or shorten every session by the
        # offset. Every method that takes a time refuses one -- including the
        # second sign-in of a user who is already there, which is the path
        # that builds no record and so has nothing else to catch it.
        naive = NOW.replace(tzinfo=None)
        async with self.opened() as store:
            user = await store.user_at_sign_in("google", "1", name=None, email=None, now=NOW)

            with pytest.raises(InvalidValueError):
                await store.user_at_sign_in("google", "1", name=None, email=None, now=naive)
            with pytest.raises(InvalidValueError):
                await store.add_session(
                    secret_hash("s"), user.id, created_at=naive, expires_at=MUCH_LATER
                )
            with pytest.raises(InvalidValueError):
                await store.session_by_hash(secret_hash("s"), now=naive)
            with pytest.raises(InvalidValueError):
                await store.delete_expired_sessions(now=naive)
            with pytest.raises(InvalidValueError):
                await store.add_pending_login(secret_hash("a"), pending(), limit=10, now=naive)
            with pytest.raises(InvalidValueError):
                await store.count_pending_logins(now=naive)
            with pytest.raises(InvalidValueError):
                await store.delete_expired_pending_logins(now=naive)

    # Two things at once.

    @asyncio_test
    async def test_one_of_several_callbacks_at_once_gets_the_sign_in(self) -> None:
        # The state is single use, and that is what stops a callback being
        # replayed: two arriving together must not both be honoured.
        async with self.opened() as store:
            await store.add_pending_login(secret_hash("state"), pending(), limit=10, now=NOW)

            taken = await asyncio.gather(
                *(store.take_pending_login(secret_hash("state")) for _ in range(5))
            )

            assert sum(1 for login in taken if login is not None) == 1
            assert await store.take_pending_login(secret_hash("state")) is None

    @asyncio_test
    async def test_the_cap_holds_when_sign_ins_begin_at_once(self) -> None:
        # Counting and storing are one step, or a flood passes the cap in the
        # gap between them -- which is the whole of what the cap is for.
        async with self.opened() as store:

            stored = await asyncio.gather(
                *(
                    store.add_pending_login(secret_hash(f"state-{n}"), pending(), limit=2, now=NOW)
                    for n in range(8)
                )
            )

            assert sum(1 for was_stored in stored if was_stored) == 2
            assert await store.count_pending_logins(now=NOW) == 2

    @asyncio_test
    async def test_one_person_signing_in_at_once_is_one_user(self) -> None:
        # Two browsers, two callbacks, one person: two user records would
        # split their conversations between two accounts for good.
        async with self.opened() as store:

            users = await asyncio.gather(
                *(
                    store.user_at_sign_in("google", "1", name=f"Ada {n}", email=None, now=NOW)
                    for n in range(5)
                )
            )

            assert len({user.id for user in users}) == 1
            assert await store.user_by_id(users[0].id) is not None
            again = await store.user_at_sign_in(
                "google", "1", name="Ada", email=None, now=MUCH_LATER
            )
            assert again.id == users[0].id

    @asyncio_test
    async def test_one_of_several_sign_outs_at_once_ends_the_session(self) -> None:
        async with self.opened() as store:
            user = await store.user_at_sign_in("google", "1", name=None, email=None, now=NOW)
            await store.add_session(
                secret_hash("s"), user.id, created_at=NOW, expires_at=MUCH_LATER
            )

            ended = await asyncio.gather(
                *(store.delete_session(secret_hash("s")) for _ in range(5))
            )

            assert sum(1 for was_ended in ended if was_ended) == 1
            assert await store.session_by_hash(secret_hash("s"), now=NOW) is None

    @asyncio_test
    async def test_a_sign_in_is_taken_while_the_expired_ones_are_swept(self) -> None:
        async with self.opened() as store:
            await store.add_pending_login(
                secret_hash("live"), pending(expires_at=MUCH_LATER), limit=10, now=NOW
            )
            await store.add_pending_login(secret_hash("stale"), pending(), limit=10, now=NOW)

            taken, swept = await asyncio.gather(
                store.take_pending_login(secret_hash("live")),
                store.delete_expired_pending_logins(now=LATER),
            )

            assert taken is not None
            assert swept == 1
            assert await store.count_pending_logins(now=NOW) == 0

    @asyncio_test
    async def test_a_session_is_found_while_the_expired_ones_are_swept(self) -> None:
        async with self.opened() as store:
            user = await store.user_at_sign_in("google", "1", name=None, email=None, now=NOW)
            await store.add_session(
                secret_hash("live"), user.id, created_at=NOW, expires_at=MUCH_LATER
            )
            await store.add_session(secret_hash("stale"), user.id, created_at=NOW, expires_at=LATER)

            found, swept = await asyncio.gather(
                store.session_by_hash(secret_hash("live"), now=NOW),
                store.delete_expired_sessions(now=LATER),
            )

            assert found is not None
            assert swept == 1
            assert await store.session_by_hash(secret_hash("stale"), now=NOW) is None
