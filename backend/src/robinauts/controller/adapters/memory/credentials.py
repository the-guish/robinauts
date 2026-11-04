# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The credentials over dicts, resolving to the users of the store they are built over.

No operation awaits, so each is one step of the event loop: of two coroutines taking one
pending login, claiming one pairing or approving it, the second finds what the first did.
"""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import Iterable
from datetime import datetime

from robinauts.controller.adapters.memory.store import MemoryStore
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


class MemoryCredentials(Credentials):
    def __init__(self, store: MemoryStore) -> None:
        self._store = store
        self._sessions: dict[str, UserSession] = {}
        self._pending: dict[str, PendingLogin] = {}
        self._tokens: dict[str, ApiToken] = {}
        self._devices: dict[str, DeviceSession] = {}
        self._pairings: dict[uuid.UUID, Pairing] = {}

    async def add_user_session(self, session: UserSession) -> None:
        self._sessions[session.secret_hash] = session

    async def resolve_user_session(
        self, secret_hash: str, now: datetime
    ) -> tuple[User, UserSession] | None:
        found = self._sessions.get(secret_hash)
        if found is None or found.expires_at <= now:
            return None
        user = self._store.user_by_id(found.user_id)
        return None if user is None else (user, found)

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

    # --- pairings -----------------------------------------------------------

    async def add_pairing(self, pairing: Pairing, now: datetime, *, at_most: int) -> bool:
        mine = [p for p in self._pairings.values() if p.user_id == pairing.user_id]
        for expired in [p for p in mine if p.expires_at <= now]:
            del self._pairings[expired.id]
        if len(mine) - sum(p.expires_at <= now for p in mine) >= at_most:
            return False
        self._pairings[pairing.id] = pairing
        return True

    async def claim_pairing(
        self,
        code_hash: str,
        claim_hash: str,
        number: str,
        device: DeviceDescription,
        now: datetime,
        new_expires_at: datetime,
    ) -> Pairing | None:
        found = next((p for p in self._pairings.values() if p.code_hash == code_hash), None)
        if found is None or found.state is not PairingState.WAITING or found.expires_at <= now:
            return None
        claimed = dataclasses.replace(
            found,
            state=PairingState.CLAIMED,
            claim_hash=claim_hash,
            match_number=number,
            device=device,
            expires_at=new_expires_at,
        )
        self._pairings[found.id] = claimed
        return claimed

    async def pairing_of(self, user_id: uuid.UUID, pairing_id: uuid.UUID) -> Pairing | None:
        found = self._pairings.get(pairing_id)
        return found if found is not None and found.user_id == user_id else None

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
        found = await self.pairing_of(user_id, pairing_id)
        if found is None or found.expires_at <= now:
            return Approval.GONE, None
        if found.state is not PairingState.CLAIMED:
            return Approval.NOT_CLAIMED, None
        assert found.claim_hash is not None
        assert found.device is not None
        if found.match_number != number:
            if found.wrong_tries + 1 >= PAIRING_WRONG_TRIES:
                del self._pairings[found.id]
                return Approval.GONE, None
            self._pairings[found.id] = dataclasses.replace(found, wrong_tries=found.wrong_tries + 1)
            return Approval.WRONG_NUMBER, None
        device = DeviceSession(
            device_id, user_id, found.device, found.claim_hash, now, now, device_expires_at
        )
        self._devices[device.secret_hash] = device
        self._pairings[found.id] = dataclasses.replace(
            found, state=PairingState.APPROVED, claim_hash=None
        )
        return Approval.APPROVED, device

    async def delete_pairing(self, user_id: uuid.UUID, pairing_id: uuid.UUID) -> bool:
        if await self.pairing_of(user_id, pairing_id) is None:
            return False
        del self._pairings[pairing_id]
        return True

    async def resolve_claim(self, claim_hash: str, now: datetime) -> PairingState | None:
        found = next((p for p in self._pairings.values() if p.claim_hash == claim_hash), None)
        return None if found is None or found.expires_at <= now else found.state

    # --- device sessions ----------------------------------------------------

    async def add_device_session(self, device: DeviceSession) -> None:
        self._devices[device.secret_hash] = device

    async def resolve_device_session(
        self, secret_hash: str, now: datetime
    ) -> tuple[User, DeviceSession] | None:
        found = self._devices.get(secret_hash)
        if found is None or found.expires_at <= now:
            return None
        user = self._store.user_by_id(found.user_id)
        if user is None:
            return None
        if found.last_used_at <= now - LAST_USED_EVERY:
            found = self._devices[secret_hash] = dataclasses.replace(found, last_used_at=now)
        return user, found

    async def device_sessions_of(self, user_id: uuid.UUID) -> list[DeviceSession]:
        return _oldest_first(d for d in self._devices.values() if d.user_id == user_id)

    async def delete_device_session(self, user_id: uuid.UUID, device_id: uuid.UUID) -> bool:
        found = next(
            (d for d in self._devices.values() if d.id == device_id and d.user_id == user_id), None
        )
        if found is None:
            return False
        del self._devices[found.secret_hash]
        return True

    async def all_device_sessions(self) -> list[tuple[User, DeviceSession]]:
        return [
            (user, device)
            for device in _oldest_first(self._devices.values())
            if (user := self._store.user_by_id(device.user_id)) is not None
        ]

    async def users_with_email(self, email: str) -> list[User]:
        found = [u for u in self._store.all_users() if u.email and u.email.lower() == email.lower()]
        return sorted(found, key=lambda u: (u.created_at, u.id))

    async def delete_device_sessions(
        self,
        *,
        user_id: uuid.UUID | None = None,
        device_id: uuid.UUID | None = None,
        every: bool = False,
    ) -> list[DeviceSession]:
        if [user_id is not None, device_id is not None, every].count(True) != 1:
            raise ValueError("one of user_id, device_id and every")
        ended = _oldest_first(
            d for d in self._devices.values() if every or d.user_id == user_id or d.id == device_id
        )
        for device in ended:
            del self._devices[device.secret_hash]
        return ended

    # --- every credential of a person ---------------------------------------

    async def delete_credentials_of(self, user_id: uuid.UUID) -> int:
        deleted = 0
        for table in (self._sessions, self._tokens, self._devices, self._pairings):
            for key in [k for k, record in table.items() if record.user_id == user_id]:
                del table[key]
                deleted += 1
        return deleted

    async def delete_credentials_of_identity(self, provider: str, subject: str) -> int:
        user = next(
            (u for u in self._store.all_users() if (u.provider, u.subject) == (provider, subject)),
            None,
        )
        return 0 if user is None else await self.delete_credentials_of(user.id)


def _oldest_first(devices: Iterable[DeviceSession]) -> list[DeviceSession]:
    return sorted(devices, key=lambda d: (d.created_at, d.id))
