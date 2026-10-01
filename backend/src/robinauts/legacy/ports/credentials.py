# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Where users, their sessions and the sign-ins in progress are kept.

Three things live behind this port: the user records the platform owns, the
sessions that say a browser is signed in, and the short-lived rows a sign-in
leaves between the button and the callback.

**What is hashed, and what is not.** The two secrets a browser carries -- the
value of a session cookie and the ``state`` of a sign-in in progress -- never
reach this port at all: it is handed their SHA-256, as
``robinauts.legacy.core.hashing.secret_hash`` gives it, and finds rows by that alone.
What a row *holds* is another matter, and a pending sign-in holds its
``nonce`` and its PKCE verifier in the clear, because the callback has to
compare them with what the provider sends and a hash cannot be compared with
something it has not seen.

That is what makes a stolen table worth nothing all the same. A session row
holds no secret, so it opens no session: the cookie is the secret, and the
hash of it does not go back the other way. A pending sign-in row can only be
found by the hash of a ``state`` that was never stored, so its ``nonce`` and
verifier belong to a sign-in whoever reads the table cannot name -- and to
finish one they would also need the authorization code, which the provider
sends to the browser that began it, at the redirect URI registered for this
deployment. The contract suite checks the part a test can check: no raw
secret is anywhere in what the store holds.

**The store keeps no clock.** Every expiry is computed by the application from
the ``Clock`` port and given here as an aware datetime, and every method that
has to know the time is told it. One clock decides what has expired, so a test
moves time by setting a fake, and a database whose clock drifts from the
process's does not quietly lengthen or shorten a session. The contract suite
in ``tests/contracts/credential_store.py`` is what both the in-memory fake and
the real store must satisfy.

Every method is one atomic operation: a caller cannot be left between two of
them with a row half stored or half taken.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from datetime import datetime

from robinauts.legacy.domain import PendingLogin, Session, User


class CredentialStore(ABC):
    """Durable storage for users, sessions and pending sign-ins."""

    # Users.

    @abstractmethod
    async def user_at_sign_in(
        self,
        provider: str,
        subject: str,
        *,
        name: str | None,
        email: str | None,
        now: datetime,
    ) -> User:
        """The user of ``(provider, subject)``, created if this is their first sign-in.

        Finding and creating are one step: two sign-ins of one person at once
        leave one user, not two, and both calls return it. An existing user
        keeps their id and ``created_at``; ``name`` and ``email`` are replaced
        by what is given, ``None`` included, because they are what the
        provider said this time.

        ``email`` is an address the provider **verified**, or ``None``: the
        caller decides that (``robinauts.legacy.core.allow.verified_email``), so that
        an unverified address never becomes a stored one. The store mints the
        id of a user it creates and dates it ``now``.
        """
        raise NotImplementedError

    @abstractmethod
    async def user_by_key(self, provider: str, subject: str) -> User | None:
        """The user of ``(provider, subject)``, or ``None`` if there is none.

        **It reads and writes nothing.** ``user_at_sign_in`` is the one that
        may create a user, and it rewrites the name and the address every time
        it is called, because a sign-in is when the provider says what they
        are now. A caller that only wants to know who somebody already is --
        the local development mode, asking on every request -- would otherwise
        pay for a row version, and its dead tuple, to be told what it could
        have read.
        """
        raise NotImplementedError

    @abstractmethod
    async def user_by_id(self, user_id: uuid.UUID) -> User | None:
        """The user with that id, or ``None`` if there is none."""
        raise NotImplementedError

    # Sessions.

    @abstractmethod
    async def add_session(
        self,
        secret_hash: str,
        user_id: uuid.UUID,
        *,
        created_at: datetime,
        expires_at: datetime,
    ) -> Session:
        """Store a session for that user, found by ``secret_hash``, and return it.

        The store mints the session's id. A session is never renewed, so
        ``expires_at`` is decided once, here.
        """
        raise NotImplementedError

    @abstractmethod
    async def session_by_hash(self, secret_hash: str, *, now: datetime) -> Session | None:
        """The session with that hash, unless there is none or it has expired at ``now``.

        An expired session is as good as absent: it is never returned, whether
        or not a sweep has reached it yet.
        """
        raise NotImplementedError

    @abstractmethod
    async def delete_session(self, secret_hash: str) -> bool:
        """Delete the session with that hash; whether there was one. Signing out."""
        raise NotImplementedError

    @abstractmethod
    async def delete_expired_sessions(self, *, now: datetime) -> int:
        """Delete every session that has expired at ``now``; how many there were."""
        raise NotImplementedError

    # Sign-ins in progress.

    @abstractmethod
    async def add_pending_login(
        self, state_hash: str, login: PendingLogin, *, limit: int, now: datetime
    ) -> bool:
        """Store a sign-in in progress under the hash of its ``state``.

        Unless ``limit`` sign-ins are in progress already, not counting those
        expired at ``now``: then nothing is stored and this returns ``False``,
        which the application turns into ``busy``. Counting and storing are one
        step, or a flood of sign-ins would pass the cap between the two.

        ``login.expires_at`` says when it stops being usable.

        A hash already in use is **refused** the same way, with ``False`` and
        the row that is there left alone. Two sign-ins cannot really collide
        -- a ``state`` carries 256 random bits -- so this is about what a
        store does when the impossible is asked of it: keeping the sign-in
        someone is in the middle of is the safe half of the choice, and
        overwriting it would end theirs.
        """
        raise NotImplementedError

    @abstractmethod
    async def take_pending_login(self, state_hash: str) -> PendingLogin | None:
        """Remove the sign-in stored under that hash and return it, or ``None``.

        Reading and removing are one step: of two callbacks arriving with one
        ``state``, exactly one is given the sign-in, which is what makes it
        single use.

        An expired sign-in is returned like any other, and removed like any
        other; the caller compares ``expires_at`` with its own clock and
        refuses it. Expiry is one decision, made in one place.
        """
        raise NotImplementedError

    @abstractmethod
    async def count_pending_logins(self, *, now: datetime) -> int:
        """How many sign-ins are in progress and unexpired at ``now``."""
        raise NotImplementedError

    @abstractmethod
    async def delete_expired_pending_logins(self, *, now: datetime) -> int:
        """Delete every sign-in that has expired at ``now``; how many there were."""
        raise NotImplementedError
