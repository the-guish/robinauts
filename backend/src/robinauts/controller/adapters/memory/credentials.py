# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The credentials over dicts, resolving to the users of the store they are built over.

No operation awaits, so each is one step of the event loop: of two coroutines taking one
pending login, the second finds it gone.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from robinauts.controller.adapters.memory.store import MemoryStore
from robinauts.controller.contract.domain import ApiToken, PendingLogin, User, UserSession
from robinauts.controller.contract.ports import Credentials


class MemoryCredentials(Credentials):
    def __init__(self, store: MemoryStore) -> None:
        self._store = store
        self._sessions: dict[str, UserSession] = {}
        self._pending: dict[str, PendingLogin] = {}
        self._tokens: dict[str, ApiToken] = {}

    async def add_user_session(self, session: UserSession) -> None:
        self._sessions[session.secret_hash] = session

    async def resolve_user_session(self, secret_hash: str, now: datetime) -> User | None:
        found = self._sessions.get(secret_hash)
        if found is None or found.expires_at <= now:
            return None
        return self._store.user_by_id(found.user_id)

    async def delete_user_session(self, secret_hash: str) -> bool:
        return self._sessions.pop(secret_hash, None) is not None

    async def add_pending_login(self, login: PendingLogin, now: datetime) -> None:
        for expired in [p for p in self._pending.values() if p.expires_at <= now]:
            del self._pending[expired.state_hash]
        self._pending[login.state_hash] = login

    async def take_pending_login(self, state_hash: str, now: datetime) -> PendingLogin | None:
        taken = self._pending.pop(state_hash, None)
        return taken if taken is not None and taken.expires_at > now else None

    async def add_api_token(self, token: ApiToken) -> None:
        self._tokens[token.secret_hash] = token

    async def resolve_api_token(self, secret_hash: str, now: datetime) -> User | None:
        found = self._tokens.get(secret_hash)
        if found is None or found.expires_at <= now:
            return None
        return self._store.user_by_id(found.user_id)

    async def api_tokens_of(self, user_id: uuid.UUID) -> list[ApiToken]:
        mine = [t for t in self._tokens.values() if t.user_id == user_id]
        return sorted(mine, key=lambda t: (t.created_at, t.id))

    async def delete_api_token(self, user_id: uuid.UUID, token_id: uuid.UUID) -> bool:
        found = next(
            (t for t in self._tokens.values() if t.id == token_id and t.user_id == user_id), None
        )
        if found is None:
            return False
        del self._tokens[found.secret_hash]
        return True

    async def delete_expired(self, now: datetime) -> int | None:
        deleted = 0
        for held in (self._sessions, self._pending, self._tokens):
            for key in [k for k, v in held.items() if v.expires_at <= now]:
                del held[key]
                deleted += 1
        return deleted
