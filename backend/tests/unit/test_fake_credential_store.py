# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The in-memory credential store, against the contract every store must meet.

And the contract itself, against a store built the way one is built by
accident -- read, then write, with an await in between. That store keeps every
promise a test can make one caller at a time, and breaks every promise the
port makes about two: it is here so that the concurrency tests are known to
have teeth before the PostgreSQL store is certified by them.
"""

from __future__ import annotations

import asyncio
import dataclasses
import uuid
from datetime import datetime

import pytest

from contracts.credential_store import CredentialStoreContract
from fakes import MemoryCredentialStore
from robinauts.legacy.domain import PendingLogin, User
from robinauts.legacy.ports import CredentialStore


class MemoryStoreContract(CredentialStoreContract):
    """The contract, over the in-memory store; the subclass below is collected."""

    async def new_store(self) -> CredentialStore:
        return MemoryCredentialStore()

    async def dump(self, store: CredentialStore) -> str:
        # Field by field, not repr: a repr leaves out what a dataclass hides,
        # and what is hidden is exactly where a secret would be.
        assert isinstance(store, MemoryCredentialStore)
        return store.everything()


class TestMemoryCredentialStore(MemoryStoreContract):
    """The fake passes what the PostgreSQL store will be held to."""


class RacyCredentialStore(MemoryCredentialStore):
    """Right for one caller, wrong for two: every operation reads, waits, writes.

    Nothing here is far-fetched. It is what a store looks like when a
    ``SELECT`` and the ``DELETE``, or a count and the ``INSERT``, are two
    statements with an ``await`` between them and no transaction around them.
    """

    async def take_pending_login(self, state_hash: str) -> PendingLogin | None:
        found = self._logins.get(state_hash)
        await asyncio.sleep(0)
        self._logins.pop(state_hash, None)
        return found

    async def add_pending_login(
        self, state_hash: str, login: PendingLogin, *, limit: int, now: datetime
    ) -> bool:
        alive = sum(1 for kept in self._logins.values() if not kept.has_expired(now))
        await asyncio.sleep(0)
        if alive >= limit:
            return False
        self._logins[state_hash] = login
        return True

    async def user_at_sign_in(
        self,
        provider: str,
        subject: str,
        *,
        name: str | None,
        email: str | None,
        now: datetime,
    ) -> User:
        key = (provider, subject)
        found = self._users.get(key)
        await asyncio.sleep(0)
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

    async def delete_session(self, secret_hash: str) -> bool:
        found = secret_hash in self._sessions
        await asyncio.sleep(0)
        self._sessions.pop(secret_hash, None)
        return found


class RacyContract(MemoryStoreContract):
    """The whole contract, over the racy store. Not collected: it is run by hand."""

    async def new_store(self) -> CredentialStore:
        return RacyCredentialStore()


CONCURRENT = [
    "test_one_of_several_callbacks_at_once_gets_the_sign_in",
    "test_the_cap_holds_when_sign_ins_begin_at_once",
    "test_one_person_signing_in_at_once_is_one_user",
    "test_one_of_several_sign_outs_at_once_ends_the_session",
]

ONE_AT_A_TIME = [
    "test_the_two_secrets_a_browser_carries_are_kept_as_hashes",
    "test_a_sign_in_is_taken_once",
    "test_the_cap_refuses_a_sign_in_and_stores_nothing",
    "test_the_second_sign_in_finds_the_user_and_refreshes_what_changed",
    "test_a_deleted_session_is_gone_and_deleting_it_twice_says_so",
]


@pytest.mark.parametrize("check", CONCURRENT)
def test_the_contract_refuses_a_store_that_is_not_atomic(check: str) -> None:
    with pytest.raises(AssertionError):
        getattr(RacyContract(), check)()


@pytest.mark.parametrize("check", ONE_AT_A_TIME)
def test_and_would_not_have_noticed_it_one_caller_at_a_time(check: str) -> None:
    # Which is the point of the four above: these four pass against it.
    getattr(RacyContract(), check)()
