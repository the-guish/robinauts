# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The credential store in dictionaries: what the real one must behave like.

It is the ``CredentialStore`` of ``robinauts.legacy.ports``, and it passes the same
contract suite (``tests/contracts/credential_store.py``) that the PostgreSQL
store will. Everything is under one lock, which is how a single operation of
the port stays atomic when two sign-ins overlap.

Each operation that reads before it writes gives the event loop a turn between
the two, under the lock. Nothing needs it; it is there so that the concurrency
tests of the contract really do interleave against this store, rather than
passing because dictionaries are quick. A store whose lock did not cover the
whole operation would fail them here, as it would against a database.

What a real store refuses, this one refuses too, and with the same error: a
key that is not the hash of a secret, a hash a session already holds, a
session for somebody who is not a user, a time that names no instant. In
PostgreSQL those are constraints and a naive datetime read as UTC; here they
are the four checks below. The point is not that a dictionary needs them --
it is that a test passing against this store means something about the other.
"""

from __future__ import annotations

import asyncio
import dataclasses
import re
import uuid
from datetime import datetime

from robinauts.legacy.domain import InvalidValueError, PendingLogin, Session, User
from robinauts.legacy.ports import CredentialStore

HASH = re.compile(r"^[0-9a-f]{64}\Z")
"""What ``robinauts.legacy.core.secret_hash`` writes, and the only key stored.

The same shape the PostgreSQL schema puts a CHECK on. ``\\Z`` rather than
``$``: ``$`` would let a trailing newline through, and a key with a newline
in it is exactly the sort of thing worth refusing.
"""


class MemoryCredentialStore(CredentialStore):
    """Users, sessions and pending sign-ins in dictionaries."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._users: dict[tuple[str, str], User] = {}
        self._sessions: dict[str, Session] = {}
        self._logins: dict[str, PendingLogin] = {}
        self.sweep_error: BaseException | None = None
        """What the two deletes of expired rows raise, if a test wants them to.

        A statement timeout, a lock held by something else, a replica that is
        read-only for the minute: housekeeping is where a store says no.
        """

    # What a test looks at: the rows themselves, to prove what is in them.

    @property
    def users(self) -> list[User]:
        return list(self._users.values())

    @property
    def sessions(self) -> dict[str, Session]:
        return dict(self._sessions)

    @property
    def pending_logins(self) -> dict[str, PendingLogin]:
        return dict(self._logins)

    def everything(self) -> str:
        """Every field of every record held, named, one row to a line.

        Not a ``repr``: a record that hides a field in its repr hides it here
        too, and what is hidden is exactly where a secret would be found. This
        is what the contract suite searches for a raw secret.
        """
        rows = [_fields(user) for user in self._users.values()]
        rows += [f"{kept}: {_fields(session)}" for kept, session in self._sessions.items()]
        rows += [f"{kept}: {_fields(login)}" for kept, login in self._logins.items()]
        return "\n".join(rows)

    def plant(self, state_hash: str, login: PendingLogin) -> None:
        """Put a sign-in row there, whatever is there already.

        The port refuses to overwrite one, so this is the only way a test can
        write the row that somebody who reached the database might have, and
        see what the application makes of reading it.
        """
        self._logins[state_hash] = login

    # Users.

    async def user_at_sign_in(
        self,
        provider: str,
        subject: str,
        *,
        name: str | None,
        email: str | None,
        now: datetime,
    ) -> User:
        _instant(now, "now")
        async with self._lock:
            key = (provider, subject)
            found = self._users.get(key)
            await _a_turn()
            user = (
                dataclasses.replace(found, name=name, email=email)
                if found is not None
                else User(
                    id=uuid.uuid4(),
                    provider=provider,
                    subject=subject,
                    name=name,
                    email=email,
                    created_at=now,
                )
            )
            self._users[key] = user
            return user

    async def user_by_key(self, provider: str, subject: str) -> User | None:
        async with self._lock:
            return self._users.get((provider, subject))

    async def user_by_id(self, user_id: uuid.UUID) -> User | None:
        async with self._lock:
            for user in self._users.values():
                if user.id == user_id:
                    return user
            return None

    # Sessions.

    async def add_session(
        self,
        secret_hash: str,
        user_id: uuid.UUID,
        *,
        created_at: datetime,
        expires_at: datetime,
    ) -> Session:
        _hashed(secret_hash, "a session")
        async with self._lock:
            if secret_hash in self._sessions:
                raise InvalidValueError("a session is already stored under that hash")
            if not any(user.id == user_id for user in self._users.values()):
                raise InvalidValueError(f"there is no user {user_id} to give a session to")
            session = Session(
                id=uuid.uuid4(),
                user_id=user_id,
                created_at=created_at,
                expires_at=expires_at,
            )
            self._sessions[secret_hash] = session
            return session

    async def session_by_hash(self, secret_hash: str, *, now: datetime) -> Session | None:
        _instant(now, "now")
        async with self._lock:
            found = self._sessions.get(secret_hash)
            if found is None or found.has_expired(now):
                return None
            return found

    async def delete_session(self, secret_hash: str) -> bool:
        async with self._lock:
            found = secret_hash in self._sessions
            await _a_turn()
            return self._sessions.pop(secret_hash, None) is not None and found

    async def delete_expired_sessions(self, *, now: datetime) -> int:
        _instant(now, "now")
        if self.sweep_error is not None:
            raise self.sweep_error
        async with self._lock:
            gone = [key for key, session in self._sessions.items() if session.has_expired(now)]
            for key in gone:
                del self._sessions[key]
            return len(gone)

    # Sign-ins in progress.

    async def add_pending_login(
        self, state_hash: str, login: PendingLogin, *, limit: int, now: datetime
    ) -> bool:
        _hashed(state_hash, "a sign-in in progress")
        _instant(now, "now")
        async with self._lock:
            if state_hash in self._logins:
                return False
            alive = sum(1 for kept in self._logins.values() if not kept.has_expired(now))
            await _a_turn()
            if alive >= limit or state_hash in self._logins:
                return False
            self._logins[state_hash] = login
            return True

    async def take_pending_login(self, state_hash: str) -> PendingLogin | None:
        async with self._lock:
            found = self._logins.get(state_hash)
            await _a_turn()
            return self._logins.pop(state_hash, None) if found is not None else None

    async def count_pending_logins(self, *, now: datetime) -> int:
        _instant(now, "now")
        async with self._lock:
            return sum(1 for kept in self._logins.values() if not kept.has_expired(now))

    async def delete_expired_pending_logins(self, *, now: datetime) -> int:
        _instant(now, "now")
        if self.sweep_error is not None:
            raise self.sweep_error
        async with self._lock:
            gone = [key for key, login in self._logins.items() if login.has_expired(now)]
            for key in gone:
                del self._logins[key]
            return len(gone)


def _hashed(key: str, what: str) -> str:
    """``key`` if it is the hash of a secret; ``InvalidValueError`` if not."""
    if not isinstance(key, str) or not HASH.match(key):
        raise InvalidValueError(f"{what} is found by the SHA-256 of a secret, not by {key!r}")
    return key


def _instant(when: datetime, what: str) -> datetime:
    """``when`` if it names an instant; ``InvalidValueError`` if it does not."""
    if not isinstance(when, datetime) or when.tzinfo is None:
        raise InvalidValueError(f"{what} must be an aware datetime, not {when!r}")
    return when


async def _a_turn() -> None:
    """Let the event loop run something else, in the middle of an operation."""
    await asyncio.sleep(0)


def _fields(record: object) -> str:
    """Every field of a record, named, whatever its repr chooses to show."""
    return ", ".join(
        f"{field.name}={getattr(record, field.name)!r}"
        for field in dataclasses.fields(record)  # type: ignore[arg-type]
    )
