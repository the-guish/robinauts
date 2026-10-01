# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The credential store on PostgreSQL: users, sessions, sign-ins in progress.

``robinauts.legacy.ports.CredentialStore`` over an ``asyncpg`` pool it is **given**.
It opens nothing and closes nothing: the pool is made in the composition root
and outlives every store (``docs/layout.md``). It is held to the same
contract suite as the in-memory fake, ``tests/contracts/credential_store.py``,
which is where the promises below are written down and checked.

**Every method is one statement.** Not because one statement is quick, but
because a read followed by a write is not atomic and the port promises that
it is: a sign-in taken by one of two callbacks, one user for one person
signing in twice, one sign-out of five. So the take is a ``DELETE ...
RETURNING``, get-or-create is an ``INSERT ... ON CONFLICT ... DO UPDATE ...
RETURNING``, and the sweeps are a ``DELETE`` wrapped in a counting CTE.

**The cap is the exception, and it is an advisory lock.** Storing a sign-in
unless ``limit`` are in progress cannot be one statement that is also
correct: ``SELECT count(*)`` sees committed rows only, so under
``READ COMMITTED`` -- at any isolation level, in fact -- ten transactions
inserting at once each count nine and all ten get in. The two ways out are
``SERIALIZABLE`` with a retry loop, and a transaction-scoped advisory lock
around the count and the insert. This takes the lock, for three reasons:
it is exact on the first try rather than after an unbounded number of
retries; ``SERIALIZABLE`` would have to be set on a pooled connection and
put back, and a forgotten reset would silently change the isolation of
everything else that connection later does; and a retry loop is a second
piece of concurrency to get right in a place where being wrong is a way past
the cap. The cost is that sign-ins beginning at the same instant queue
behind each other for the length of one insert, which is the right side to
be wrong on for something that happens once per sign-in.

The lock's key is the ``pending_logins`` table's own OID in a number space
of ours, so that two deployments sharing a database -- or two test runs in
schemas of their own -- do not queue behind each other.

**Times come in, and go out, as instants.** Every deadline is computed by
the application from its ``Clock`` and passed in; nothing here calls
``now()``. ``timestamptz`` columns and asyncpg's binary protocol mean an
aware datetime goes in as an instant and comes back as an aware datetime,
whatever zone the server, the session or the caller is in. A naive datetime
is refused rather than silently read as UTC.

**A key is checked here, not by the database.** The port is handed the
SHA-256 of a secret and never the secret, and a store that wrote down
whatever it was given would keep a secret in the clear the day a caller
passed the wrong one. The column has a CHECK for that, but a constraint only
runs when a row is really inserted, and ``add_pending_login`` has a path --
the cap is reached -- that inserts nothing: there the CHECK never fires and
the caller would be told "busy" about a key that was never a key. So the
shape is checked in Python first, before either statement, exactly as the
time is.

**Driver errors are left alone, except two that are answers.** A connection
that drops, a statement that times out, a server that is restarting: none of
those is a domain error, and turning one into one would tell the operator to
fix the wrong thing. What *is* translated is the pair of constraints that
exist to say no to a caller, and the contract suite requires the same no
from the in-memory fake: a secret hash a session already holds and a session
for a user who is not there, both ``InvalidValueError``.

They are told apart **by constraint name**, not by exception class. The
class says "a unique index somewhere in this statement", and the day a
second one is added to ``sessions`` every violation of it would be reported
as the first -- "a session is already stored under that hash", about a row
where the hash was fine. The names are written into ``schema.sql`` for that
reason. A constraint this code does not know is a constraint nobody planned
to violate, so it propagates as the driver error it is. The third
translation lives in ``schema.py``: a missing table at start-up is a missing
schema.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime

import asyncpg

from robinauts.legacy.domain import InvalidValueError, PendingLogin, Session, User
from robinauts.legacy.ports import CredentialStore

HASH = re.compile(r"^[0-9a-f]{64}\Z")
"""What ``robinauts.legacy.core.hashing.secret_hash`` writes, and the only key stored.

The same shape the schema puts a CHECK on, spelt again here because the
CHECK cannot speak for a statement that inserts no row. ``\\Z`` rather than
``$``: ``$`` would let a trailing newline through, and a key with a newline
in it is exactly the sort of thing worth refusing.
"""

SESSION_REFUSALS = {
    "sessions_secret_hash_key": "a session is already stored under that hash",
    "sessions_user_id_fkey": "there is no user {user_id} to give a session to",
}
"""The constraints on ``sessions`` that are an answer, by name.

Anything else -- a constraint added later, one this code has never heard of
-- is not an answer to give a caller. It propagates.
"""

_LOCK_SPACE = 1382508617
"""The high half of the advisory lock key: ours, and nobody else's.

PostgreSQL's advisory locks are one number space for the whole database,
shared with every other program connected to it. Half of a 64-bit key spent
on a constant is what keeps our lock from being somebody else's.
"""

_LOCK_PENDING_LOGINS = f"""
SELECT pg_advisory_xact_lock(
    ({_LOCK_SPACE}::bigint << 32) | 'pending_logins'::regclass::oid::bigint
)
"""
"""Hold until this transaction ends: nobody else counts or inserts meanwhile.

``regclass`` resolves through ``search_path``, so the key is this
deployment's ``pending_logins`` and not the one next door.
"""

_INSERT_PENDING_LOGIN = """
INSERT INTO pending_logins
    (state_hash, provider, nonce, verifier, return_to, created_at, expires_at)
SELECT $1, $2, $3, $4, $5, $6, $7
WHERE (SELECT count(*) FROM pending_logins WHERE expires_at > $8) < $9
ON CONFLICT (state_hash) DO NOTHING
RETURNING state_hash
"""
"""Store it unless the cap is reached or the hash is taken; say which by returning.

``WHERE ... < $9`` is the cap, counting only what has not expired at ``$8``.
``ON CONFLICT DO NOTHING`` is the hash already in use: the row that is there
is left exactly as it is, because it belongs to a sign-in somebody is in the
middle of. Either way nothing comes back, and the caller is told ``False``.
"""

_TAKE_PENDING_LOGIN = """
DELETE FROM pending_logins WHERE state_hash = $1
RETURNING provider, nonce, verifier, return_to, created_at, expires_at
"""
"""Read and remove in one step: of two callbacks with one state, one wins."""

_USER_AT_SIGN_IN = """
INSERT INTO users (provider, subject, name, email, created_at)
VALUES ($1, $2, $3, $4, $5)
ON CONFLICT (provider, subject) DO UPDATE SET name = EXCLUDED.name, email = EXCLUDED.email
RETURNING id, provider, subject, name, email, created_at
"""
"""Find or create in one step, and refresh what the provider said this time.

``DO UPDATE`` rather than ``DO NOTHING``: ``DO NOTHING`` returns no row on a
conflict, which would mean a second statement to read the user back, and two
statements is exactly what this must not be.
"""

_USER_COLUMNS = "id, provider, subject, name, email, created_at"
_SESSION_COLUMNS = "id, user_id, created_at, expires_at"


class PostgresCredentialStore(CredentialStore):
    """Users, sessions and pending sign-ins in PostgreSQL."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

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
        row = await self._pool.fetchrow(
            _USER_AT_SIGN_IN, provider, subject, name, email, _instant(now, "now")
        )
        assert row is not None  # an upsert always returns its row
        return _user(row)

    async def user_by_key(self, provider: str, subject: str) -> User | None:
        # One indexed lookup on the UNIQUE (provider, subject) of
        # ``schema.sql``, and no write: the upsert above rewrites the row
        # whatever it holds, which is right at a sign-in and wrong for
        # somebody merely asking who a user is.
        row = await self._pool.fetchrow(
            f"SELECT {_USER_COLUMNS} FROM users WHERE provider = $1 AND subject = $2",
            provider,
            subject,
        )
        return None if row is None else _user(row)

    async def user_by_id(self, user_id: uuid.UUID) -> User | None:
        row = await self._pool.fetchrow(f"SELECT {_USER_COLUMNS} FROM users WHERE id = $1", user_id)
        return None if row is None else _user(row)

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
        try:
            row = await self._pool.fetchrow(
                f"""
                INSERT INTO sessions (secret_hash, user_id, created_at, expires_at)
                VALUES ($1, $2, $3, $4)
                RETURNING {_SESSION_COLUMNS}
                """,
                secret_hash,
                user_id,
                _instant(created_at, "created_at"),
                _instant(expires_at, "expires_at"),
            )
        except asyncpg.exceptions.IntegrityConstraintViolationError as violation:
            refusal = SESSION_REFUSALS.get(violation.constraint_name or "")
            if refusal is None:
                # A constraint this code has never heard of. Nobody planned
                # to violate it, so it is a bug, and a bug arrives as itself.
                raise
            raise InvalidValueError(refusal.format(user_id=user_id)) from violation
        assert row is not None  # an insert always returns its row
        return _session(row)

    async def session_by_hash(self, secret_hash: str, *, now: datetime) -> Session | None:
        # Expired is absent, whether or not a sweep has reached it: the same
        # `now` the caller would compare against decides it here, in the
        # statement, so there is no window between finding a row and judging it.
        row = await self._pool.fetchrow(
            f"""
            SELECT {_SESSION_COLUMNS} FROM sessions
            WHERE secret_hash = $1 AND expires_at > $2
            """,
            secret_hash,
            _instant(now, "now"),
        )
        return None if row is None else _session(row)

    async def delete_session(self, secret_hash: str) -> bool:
        gone = await self._pool.fetchval(
            "DELETE FROM sessions WHERE secret_hash = $1 RETURNING true", secret_hash
        )
        return gone is not None

    async def delete_expired_sessions(self, *, now: datetime) -> int:
        return await self._swept(
            """
            WITH swept AS (DELETE FROM sessions WHERE expires_at <= $1 RETURNING 1)
            SELECT count(*) FROM swept
            """,
            now,
        )

    # Sign-ins in progress.

    async def add_pending_login(
        self, state_hash: str, login: PendingLogin, *, limit: int, now: datetime
    ) -> bool:
        # Before a connection is taken, so that a bad `now` costs nothing and
        # takes no lock.
        # Both before the connection is taken. A bad key or a bad time then
        # costs nothing and takes no lock -- and the key in particular cannot
        # wait for the column's CHECK, because the statement below inserts no
        # row at all once the cap is reached and the CHECK would never run.
        _hashed(state_hash, "a sign-in in progress")
        now = _instant(now, "now")
        async with self._pool.acquire() as connection, connection.transaction():
            # The lock ends with the transaction, however it ends: there is
            # no unlock to forget, and a caller that is cancelled or fails
            # between the count and the insert does not leave the cap held.
            await connection.execute(_LOCK_PENDING_LOGINS)
            stored = await connection.fetchval(
                _INSERT_PENDING_LOGIN,
                state_hash,
                login.provider,
                login.nonce,
                login.verifier,
                login.return_to,
                login.created_at,
                login.expires_at,
                now,
                limit,
            )
        return stored is not None

    async def take_pending_login(self, state_hash: str) -> PendingLogin | None:
        row = await self._pool.fetchrow(_TAKE_PENDING_LOGIN, state_hash)
        if row is None:
            return None
        # An expired sign-in comes back like any other, and is gone like any
        # other: the caller compares it with its own clock and refuses it.
        return PendingLogin(
            provider=row["provider"],
            nonce=row["nonce"],
            verifier=row["verifier"],
            created_at=row["created_at"],
            expires_at=row["expires_at"],
            return_to=row["return_to"],
        )

    async def count_pending_logins(self, *, now: datetime) -> int:
        return await self._pool.fetchval(
            "SELECT count(*) FROM pending_logins WHERE expires_at > $1", _instant(now, "now")
        )

    async def delete_expired_pending_logins(self, *, now: datetime) -> int:
        return await self._swept(
            """
            WITH swept AS (DELETE FROM pending_logins WHERE expires_at <= $1 RETURNING 1)
            SELECT count(*) FROM swept
            """,
            now,
        )

    async def _swept(self, statement: str, now: datetime) -> int:
        """Run a sweep and return how many rows it deleted.

        The count comes out of a CTE over the ``DELETE`` itself rather than
        out of the driver's ``DELETE 3`` status line: one is a number, the
        other is a string to be parsed and believed.
        """
        return await self._pool.fetchval(statement, _instant(now, "now"))


def _user(row: asyncpg.Record) -> User:
    return User(
        id=row["id"],
        provider=row["provider"],
        subject=row["subject"],
        name=row["name"],
        email=row["email"],
        created_at=row["created_at"],
    )


def _session(row: asyncpg.Record) -> Session:
    return Session(
        id=row["id"],
        user_id=row["user_id"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
    )


def _hashed(key: str, what: str) -> str:
    """``key`` if it is the hash of a secret; ``InvalidValueError`` if not.

    The same refusal the in-memory fake makes, and the same one the column's
    CHECK would make -- for the statements where a row is really inserted.
    """
    if not isinstance(key, str) or not HASH.match(key):
        raise InvalidValueError(f"{what} is found by the SHA-256 of a secret, not by {key!r}")
    return key


def _instant(when: datetime, what: str) -> datetime:
    """``when`` if it names an instant; ``InvalidValueError`` if it does not.

    asyncpg would take a naive datetime for a ``timestamptz`` parameter and
    read it as UTC, so a clock that lost its time zone would shorten or
    lengthen every session here by the deployment's offset and say nothing.
    The domain refuses a naive datetime inside a record for the same reason;
    this is the same refusal for the times passed beside one.
    """
    if not isinstance(when, datetime) or when.tzinfo is None:
        raise InvalidValueError(f"{what} must be an aware datetime, not {when!r}")
    return when
