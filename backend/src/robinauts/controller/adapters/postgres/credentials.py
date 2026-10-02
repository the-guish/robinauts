# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The credentials over the PostgreSQL store's pool, resolving to the users of its ``users``.

The pool is the store's, read from it at each call, since the store opens and closes it: one
pool per process. Every operation is one statement, and every time is a parameter.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from robinauts.controller.adapters.postgres.store import PostgresStore, _rows, _user
from robinauts.controller.contract.domain import ApiToken, PendingLogin, User, UserSession
from robinauts.controller.contract.ports import Credentials

_RESOLVE = """
SELECT u.id, u.provider, u.subject, u.name, u.email, u.created_at
FROM {table} AS c JOIN users AS u ON u.id = c.user_id
WHERE c.secret_hash = $1 AND c.expires_at > $2
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

    async def resolve_user_session(self, secret_hash: str, now: datetime) -> User | None:
        return await self._resolve("user_sessions", secret_hash, now)

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
