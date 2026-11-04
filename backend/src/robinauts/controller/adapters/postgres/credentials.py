# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The credentials over the PostgreSQL store's pool, resolving to the users of its ``users``.

The pool is the store's, read from it at each call, since the store opens and closes it: one
pool per process. Every operation is one statement, and every time is a parameter. Where a
statement decides by a row another may change at once (a claim, an approve), it locks that row
first, so the second waits and then sees what the first did.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import asyncpg

from robinauts.controller.adapters.postgres.store import PostgresStore, _rows, _user
from robinauts.controller.contract.domain import (
    LAST_USED_EVERY,
    PAIRING_WRONG_TRIES,
    ApiToken,
    Approval,
    DeviceDescription,
    DeviceSession,
    Pairing,
    PairingState,
    PendingLogin,
    User,
    UserSession,
)
from robinauts.controller.contract.ports import Credentials

_USER = "u.id, u.provider, u.subject, u.name, u.email, u.created_at, u.hosted_domain, u.groups"

_RESOLVE = f"""
SELECT {_USER}
FROM {{table}} AS c JOIN users AS u ON u.id = c.user_id
WHERE c.secret_hash = $1 AND c.expires_at > $2
"""

_RESOLVE_SESSION = f"""
SELECT {_USER},
       c.id AS session_id, c.secret_hash, c.created_at AS session_created_at, c.expires_at
FROM user_sessions AS c JOIN users AS u ON u.id = c.user_id
WHERE c.secret_hash = $1 AND c.expires_at > $2
"""

_DEVICE_COLUMNS = (
    "id, user_id, name, platform, app_version, secret_hash, created_at, last_used_at, expires_at"
)

_PAIRING_COLUMNS = (
    "id, user_id, code_hash, state, created_at, expires_at, claim_hash, match_number,"
    " wrong_tries, device_name, device_platform, device_app_version"
)

_ADD_PAIRING = f"""
WITH expired AS (DELETE FROM pairings WHERE user_id = $2 AND expires_at <= $7)
INSERT INTO pairings ({_PAIRING_COLUMNS})
SELECT $1, $2, $3, 'waiting', $4, $5, NULL, NULL, 0, NULL, NULL, NULL
WHERE (SELECT count(*) FROM pairings WHERE user_id = $2 AND expires_at > $7) < $6
"""
"""The count reads the rows as they were before the delete, and counts the live ones."""

_CLAIM_PAIRING = f"""
UPDATE pairings
SET state = 'claimed', claim_hash = $2, match_number = $3,
    device_name = $4, device_platform = $5, device_app_version = $6, expires_at = $8
WHERE code_hash = $1 AND state = 'waiting' AND expires_at > $7
RETURNING {_PAIRING_COLUMNS}
"""

_APPROVE_PAIRING = f"""
WITH target AS (
    SELECT id, state, claim_hash, match_number, wrong_tries,
           device_name, device_platform, device_app_version
    FROM pairings
    WHERE id = $1 AND user_id = $2 AND expires_at > $6
    FOR UPDATE
),
approved AS (
    UPDATE pairings AS p SET state = 'approved', claim_hash = NULL
    FROM target AS t
    WHERE p.id = t.id AND t.state = 'claimed' AND t.match_number = $3
    RETURNING t.claim_hash, t.device_name, t.device_platform, t.device_app_version
),
device AS (
    INSERT INTO device_sessions ({_DEVICE_COLUMNS})
    SELECT $4, $2, device_name, device_platform, device_app_version, claim_hash, $6, $6, $5
    FROM approved
    RETURNING {_DEVICE_COLUMNS}
),
wrong AS (
    UPDATE pairings AS p SET wrong_tries = p.wrong_tries + 1
    FROM target AS t
    WHERE p.id = t.id AND t.state = 'claimed' AND t.match_number <> $3
      AND t.wrong_tries + 1 < {PAIRING_WRONG_TRIES}
    RETURNING p.id
),
ended AS (
    DELETE FROM pairings AS p
    USING target AS t
    WHERE p.id = t.id AND t.state = 'claimed' AND t.match_number <> $3
      AND t.wrong_tries + 1 >= {PAIRING_WRONG_TRIES}
    RETURNING p.id
)
SELECT (SELECT state FROM target) AS state,
       EXISTS (SELECT 1 FROM wrong) AS wrong,
       d.*
FROM (SELECT 1) AS one LEFT JOIN device AS d ON true
"""
"""Exactly one of ``approved``, ``wrong`` and ``ended`` can touch the row, by their conditions
on the locked ``target``."""

_RESOLVE_DEVICE = f"""
WITH touched AS (
    UPDATE device_sessions SET last_used_at = $2
    WHERE secret_hash = $1 AND expires_at > $2 AND last_used_at <= $2 - $3::interval
    RETURNING {_DEVICE_COLUMNS}
),
device AS (
    SELECT {_DEVICE_COLUMNS} FROM touched
    UNION ALL
    SELECT {_DEVICE_COLUMNS} FROM device_sessions
    WHERE secret_hash = $1 AND expires_at > $2 AND NOT EXISTS (SELECT 1 FROM touched)
)
SELECT {_USER},
       d.id AS device_id, d.name AS device_name, d.platform, d.app_version, d.secret_hash,
       d.created_at AS device_created_at, d.last_used_at, d.expires_at
FROM device AS d JOIN users AS u ON u.id = d.user_id
"""
"""Written only when a minute has passed; otherwise read as it was, so most requests write
nothing."""

_ALL_DEVICES = f"""
SELECT {_USER},
       d.id AS device_id, d.name AS device_name, d.platform, d.app_version, d.secret_hash,
       d.created_at AS device_created_at, d.last_used_at, d.expires_at
FROM device_sessions AS d JOIN users AS u ON u.id = d.user_id
ORDER BY d.created_at, d.id
"""

_DELETE_CREDENTIALS = """
WITH sessions AS (DELETE FROM user_sessions WHERE user_id = ANY($1::uuid[]) RETURNING 1),
devices AS (DELETE FROM device_sessions WHERE user_id = ANY($1::uuid[]) RETURNING 1),
tokens AS (DELETE FROM api_tokens WHERE user_id = ANY($1::uuid[]) RETURNING 1),
pairings AS (DELETE FROM pairings WHERE user_id = ANY($1::uuid[]) RETURNING 1)
SELECT (SELECT count(*) FROM sessions) + (SELECT count(*) FROM devices)
     + (SELECT count(*) FROM tokens) + (SELECT count(*) FROM pairings)
"""

_DELETE_CREDENTIALS_OF_IDENTITY = """
WITH person AS (SELECT id FROM users WHERE provider = $1 AND subject = $2),
sessions AS (DELETE FROM user_sessions WHERE user_id IN (SELECT id FROM person) RETURNING 1),
devices AS (DELETE FROM device_sessions WHERE user_id IN (SELECT id FROM person) RETURNING 1),
tokens AS (DELETE FROM api_tokens WHERE user_id IN (SELECT id FROM person) RETURNING 1),
pairings AS (DELETE FROM pairings WHERE user_id IN (SELECT id FROM person) RETURNING 1)
SELECT (SELECT count(*) FROM sessions) + (SELECT count(*) FROM devices)
     + (SELECT count(*) FROM tokens) + (SELECT count(*) FROM pairings)
"""

_ADD_PENDING_LOGIN = """
WITH expired AS (DELETE FROM pending_logins WHERE expires_at <= $8)
INSERT INTO pending_logins
    (state_hash, provider, nonce, verifier, return_to, created_at, expires_at)
VALUES ($1, $2, $3, $4, $5, $6, $7)
"""

_TOKEN_COLUMNS = "id, user_id, name, secret_hash, created_at, expires_at"


class PostgresCredentials(Credentials):
    def __init__(self, store: PostgresStore) -> None:
        self._store = store

    async def add_user_session(self, session: UserSession) -> None:
        await self._store.pool.execute(
            "INSERT INTO user_sessions (id, user_id, secret_hash, created_at, expires_at)"
            " VALUES ($1, $2, $3, $4, $5)",
            session.id,
            session.user_id,
            session.secret_hash,
            session.created_at,
            session.expires_at,
        )

    async def resolve_user_session(
        self, secret_hash: str, now: datetime
    ) -> tuple[User, UserSession] | None:
        row = await self._store.pool.fetchrow(_RESOLVE_SESSION, secret_hash, now)
        if row is None:
            return None
        session = UserSession(
            row["session_id"],
            row["id"],
            row["secret_hash"],
            row["session_created_at"],
            row["expires_at"],
        )
        return _user(row), session

    async def delete_user_session(self, secret_hash: str) -> bool:
        status = await self._store.pool.execute(
            "DELETE FROM user_sessions WHERE secret_hash = $1", secret_hash
        )
        return _rows(status) == 1

    async def add_pending_login(self, login: PendingLogin, now: datetime) -> None:
        await self._store.pool.execute(
            _ADD_PENDING_LOGIN,
            login.state_hash,
            login.provider,
            login.nonce,
            login.verifier,
            login.return_to,
            login.created_at,
            login.expires_at,
            now,
        )

    async def take_pending_login(self, state_hash: str, now: datetime) -> PendingLogin | None:
        row = await self._store.pool.fetchrow(
            "DELETE FROM pending_logins WHERE state_hash = $1 RETURNING *", state_hash
        )
        # Judged after the delete, so an expired one is gone either way.
        if row is None or row["expires_at"] <= now:
            return None
        return PendingLogin(**row)

    async def add_api_token(self, token: ApiToken) -> None:
        await self._store.pool.execute(
            f"INSERT INTO api_tokens ({_TOKEN_COLUMNS}) VALUES ($1, $2, $3, $4, $5, $6)",
            token.id,
            token.user_id,
            token.name,
            token.secret_hash,
            token.created_at,
            token.expires_at,
        )

    async def resolve_api_token(self, secret_hash: str, now: datetime) -> User | None:
        return await self._resolve("api_tokens", secret_hash, now)

    async def api_tokens_of(self, user_id: uuid.UUID) -> list[ApiToken]:
        rows = await self._store.pool.fetch(
            f"SELECT {_TOKEN_COLUMNS} FROM api_tokens WHERE user_id = $1 ORDER BY created_at, id",
            user_id,
        )
        return [ApiToken(**row) for row in rows]

    async def delete_api_token(self, user_id: uuid.UUID, token_id: uuid.UUID) -> bool:
        status = await self._store.pool.execute(
            "DELETE FROM api_tokens WHERE id = $1 AND user_id = $2", token_id, user_id
        )
        return _rows(status) == 1

    async def _resolve(self, table: str, secret_hash: str, now: datetime) -> User | None:
        row = await self._store.pool.fetchrow(_RESOLVE.format(table=table), secret_hash, now)
        return None if row is None else _user(row)

    # --- pairings -----------------------------------------------------------

    async def add_pairing(self, pairing: Pairing, now: datetime, *, at_most: int) -> bool:
        status = await self._store.pool.execute(
            _ADD_PAIRING,
            pairing.id,
            pairing.user_id,
            pairing.code_hash,
            pairing.created_at,
            pairing.expires_at,
            at_most,
            now,
        )
        return _rows(status) == 1

    async def claim_pairing(
        self,
        code_hash: str,
        claim_hash: str,
        number: str,
        device: DeviceDescription,
        now: datetime,
        new_expires_at: datetime,
    ) -> Pairing | None:
        row = await self._store.pool.fetchrow(
            _CLAIM_PAIRING,
            code_hash,
            claim_hash,
            number,
            device.name,
            device.platform,
            device.app_version,
            now,
            new_expires_at,
        )
        return None if row is None else _pairing(row)

    async def pairing_of(self, user_id: uuid.UUID, pairing_id: uuid.UUID) -> Pairing | None:
        row = await self._store.pool.fetchrow(
            f"SELECT {_PAIRING_COLUMNS} FROM pairings WHERE id = $1 AND user_id = $2",
            pairing_id,
            user_id,
        )
        return None if row is None else _pairing(row)

    async def approve_pairing(
        self,
        user_id: uuid.UUID,
        pairing_id: uuid.UUID,
        number: str,
        *,
        device_id: uuid.UUID,
        device_expires_at: datetime,
        now: datetime,
    ) -> tuple[Approval, DeviceSession | None]:
        row = await self._store.pool.fetchrow(
            _APPROVE_PAIRING, pairing_id, user_id, number, device_id, device_expires_at, now
        )
        if row["state"] is None:
            return Approval.GONE, None
        if row["state"] != PairingState.CLAIMED:
            return Approval.NOT_CLAIMED, None
        if row["id"] is not None:
            return Approval.APPROVED, _device(row)
        return (Approval.WRONG_NUMBER if row["wrong"] else Approval.GONE), None

    async def delete_pairing(self, user_id: uuid.UUID, pairing_id: uuid.UUID) -> bool:
        status = await self._store.pool.execute(
            "DELETE FROM pairings WHERE id = $1 AND user_id = $2", pairing_id, user_id
        )
        return _rows(status) == 1

    async def resolve_claim(self, claim_hash: str, now: datetime) -> PairingState | None:
        state = await self._store.pool.fetchval(
            "SELECT state FROM pairings WHERE claim_hash = $1 AND expires_at > $2",
            claim_hash,
            now,
        )
        return None if state is None else PairingState(state)

    # --- device sessions ----------------------------------------------------

    async def add_device_session(self, device: DeviceSession) -> None:
        await self._store.pool.execute(
            f"INSERT INTO device_sessions ({_DEVICE_COLUMNS})"
            " VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)",
            device.id,
            device.user_id,
            device.device.name,
            device.device.platform,
            device.device.app_version,
            device.secret_hash,
            device.created_at,
            device.last_used_at,
            device.expires_at,
        )

    async def resolve_device_session(
        self, secret_hash: str, now: datetime
    ) -> tuple[User, DeviceSession] | None:
        row = await self._store.pool.fetchrow(_RESOLVE_DEVICE, secret_hash, now, LAST_USED_EVERY)
        return None if row is None else (_user(row), _joined_device(row))

    async def device_sessions_of(self, user_id: uuid.UUID) -> list[DeviceSession]:
        rows = await self._store.pool.fetch(
            f"SELECT {_DEVICE_COLUMNS} FROM device_sessions WHERE user_id = $1"
            " ORDER BY created_at, id",
            user_id,
        )
        return [_device(row) for row in rows]

    async def delete_device_session(self, user_id: uuid.UUID, device_id: uuid.UUID) -> bool:
        status = await self._store.pool.execute(
            "DELETE FROM device_sessions WHERE id = $1 AND user_id = $2", device_id, user_id
        )
        return _rows(status) == 1

    async def all_device_sessions(self) -> list[tuple[User, DeviceSession]]:
        rows = await self._store.pool.fetch(_ALL_DEVICES)
        return [(_user(row), _joined_device(row)) for row in rows]

    async def users_with_email(self, email: str) -> list[User]:
        rows = await self._store.pool.fetch(
            f"SELECT {_USER} FROM users AS u WHERE lower(u.email) = lower($1)"
            " ORDER BY u.created_at, u.id",
            email,
        )
        return [_user(row) for row in rows]

    async def delete_device_sessions(
        self,
        *,
        user_id: uuid.UUID | None = None,
        device_id: uuid.UUID | None = None,
        every: bool = False,
    ) -> list[DeviceSession]:
        if [user_id is not None, device_id is not None, every].count(True) != 1:
            raise ValueError("one of user_id, device_id and every")
        rows = await self._store.pool.fetch(
            "WITH ended AS (DELETE FROM device_sessions"
            " WHERE $3 OR user_id = $1 OR id = $2"
            f" RETURNING {_DEVICE_COLUMNS})"
            f" SELECT {_DEVICE_COLUMNS} FROM ended ORDER BY created_at, id",
            user_id,
            device_id,
            every,
        )
        return [_device(row) for row in rows]

    # --- every credential of a person ---------------------------------------

    async def delete_credentials_of(self, user_id: uuid.UUID) -> int:
        return await self._store.pool.fetchval(_DELETE_CREDENTIALS, [user_id])

    async def delete_credentials_of_identity(self, provider: str, subject: str) -> int:
        return await self._store.pool.fetchval(_DELETE_CREDENTIALS_OF_IDENTITY, provider, subject)


def _pairing(row: asyncpg.Record) -> Pairing:
    device = None
    if row["device_name"] is not None:
        device = DeviceDescription(
            row["device_name"], row["device_platform"], row["device_app_version"]
        )
    return Pairing(
        row["id"],
        row["user_id"],
        row["code_hash"],
        PairingState(row["state"]),
        row["created_at"],
        row["expires_at"],
        row["claim_hash"],
        row["match_number"],
        row["wrong_tries"],
        device,
    )


def _device(row: asyncpg.Record) -> DeviceSession:
    return DeviceSession(
        row["id"],
        row["user_id"],
        DeviceDescription(row["name"], row["platform"], row["app_version"]),
        row["secret_hash"],
        row["created_at"],
        row["last_used_at"],
        row["expires_at"],
    )


def _joined_device(row: asyncpg.Record) -> DeviceSession:
    """A device read beside its user, under the column names the join gave it."""
    return DeviceSession(
        row["device_id"],
        row["id"],
        DeviceDescription(row["device_name"], row["platform"], row["app_version"]),
        row["secret_hash"],
        row["device_created_at"],
        row["last_used_at"],
        row["expires_at"],
    )
