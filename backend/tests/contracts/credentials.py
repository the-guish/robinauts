# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""What every ``Credentials`` must do: the promises of the port in ``contract/ports.py``.

Subclass ``CredentialsContract`` and override ``new_credentials``, which returns empty
credentials and the store whose users they resolve to, and ``close_credentials`` when they
have something to release. No test sleeps: every expiry is a datetime the test chose.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime, timedelta
from typing import Any

from robinauts.controller.contract.domain import ApiToken, PendingLogin, User, UserSession
from robinauts.controller.contract.ports import Credentials
from robinauts.controller.ports.store import Store

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
SECOND = timedelta(seconds=1)
LOGIN_LIFE = timedelta(minutes=10)
LOGIN_ENDS = NOW + LOGIN_LIFE
SESSION_ENDS = NOW + timedelta(hours=12)
TOKEN_ENDS = NOW + timedelta(days=90)


def hashed(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def session(user: User, secret: str = "cookie") -> UserSession:
    return UserSession(uuid.uuid4(), user.id, hashed(secret), NOW, SESSION_ENDS)


def pending(state: str = "state", expires_at: datetime = LOGIN_ENDS) -> PendingLogin:
    return PendingLogin(hashed(state), "okta", "nonce", "verifier", "/#/chat", NOW, expires_at)


def token(user: User, secret: str = "token", created_at: datetime = NOW) -> ApiToken:
    return ApiToken(uuid.uuid4(), user.id, "ci", hashed(secret), created_at, TOKEN_ENDS)


def credentials_test(test: Callable[..., Coroutine[Any, Any, None]]) -> Callable[..., None]:
    """Run the test on a loop of its own, with fresh credentials, closed however it ends."""

    def run(self: CredentialsContract) -> None:
        async def go() -> None:
            credentials, store = await self.new_credentials()
            try:
                await test(self, credentials, store)
            finally:
                await self.close_credentials(credentials, store)

        asyncio.run(go())

    # The name and the doc, but not `__wrapped__`: pytest would read the wrapped signature
    # and look for fixtures called `credentials` and `store`.
    run.__name__, run.__qualname__ = test.__name__, test.__qualname__
    run.__doc__, run.__module__ = test.__doc__, test.__module__
    return run


class CredentialsContract:
    async def new_credentials(self) -> tuple[Credentials, Store]:
        raise NotImplementedError("a CredentialsContract subclass overrides `new_credentials`")

    async def close_credentials(self, credentials: Credentials, store: Store) -> None:
        """Release what they hold; nothing by default."""

    async def user(self, store: Store, subject: str = "me") -> User:
        return await store.add_user_if_absent(User(uuid.uuid4(), "okta", subject, created_at=NOW))

    # --- user sessions ------------------------------------------------------

    @credentials_test
    async def test_a_session_resolves_to_its_user_until_now_passes_its_expiry(
        self, credentials: Credentials, store: Store
    ) -> None:
        me = await self.user(store)
        await credentials.add_user_session(session(me))
        assert await credentials.resolve_user_session(hashed("cookie"), NOW) == me
        assert await credentials.resolve_user_session(hashed("cookie"), SESSION_ENDS - SECOND) == me
        assert await credentials.resolve_user_session(hashed("cookie"), SESSION_ENDS) is None
        assert await credentials.resolve_user_session(hashed("other"), NOW) is None

    @credentials_test
    async def test_a_session_nobody_has_is_not_deleted_and_a_deleted_one_does_not_resolve(
        self, credentials: Credentials, store: Store
    ) -> None:
        me = await self.user(store)
        assert await credentials.delete_user_session(hashed("cookie")) is False
        await credentials.add_user_session(session(me))
        assert await credentials.delete_user_session(hashed("cookie")) is True
        assert await credentials.resolve_user_session(hashed("cookie"), NOW) is None

    # --- pending logins -----------------------------------------------------

    @credentials_test
    async def test_of_two_takes_of_one_pending_login_at_once_one_gets_it(
        self, credentials: Credentials, store: Store
    ) -> None:
        login = pending()
        await credentials.add_pending_login(login, NOW)
        taken = await asyncio.gather(
            credentials.take_pending_login(login.state_hash, NOW),
            credentials.take_pending_login(login.state_hash, NOW),
        )
        assert [t for t in taken if t is not None] == [login]

    @credentials_test
    async def test_a_pending_login_past_its_expiry_is_not_taken(
        self, credentials: Credentials, store: Store
    ) -> None:
        login = pending()
        await credentials.add_pending_login(login, NOW)
        assert await credentials.take_pending_login(login.state_hash, LOGIN_ENDS) is None
        # Deleted all the same: asked again at a time it was valid, it is gone.
        assert await credentials.take_pending_login(login.state_hash, NOW) is None

    @credentials_test
    async def test_adding_a_pending_login_deletes_the_expired_ones(
        self, credentials: Credentials, store: Store
    ) -> None:
        old = pending("old")
        kept = pending("kept", expires_at=LOGIN_ENDS + SECOND)
        await credentials.add_pending_login(old, NOW)
        await credentials.add_pending_login(kept, NOW)
        await credentials.add_pending_login(pending("new", LOGIN_ENDS + LOGIN_LIFE), LOGIN_ENDS)
        # Taken at a time the old one was valid, so only its deletion can have removed it.
        assert await credentials.take_pending_login(old.state_hash, NOW) is None
        assert await credentials.take_pending_login(kept.state_hash, NOW) == kept

    # --- API tokens ---------------------------------------------------------

    @credentials_test
    async def test_a_token_resolves_until_revoked_and_not_past_its_expiry(
        self, credentials: Credentials, store: Store
    ) -> None:
        me = await self.user(store)
        mine = token(me)
        await credentials.add_api_token(mine)
        assert await credentials.resolve_api_token(hashed("token"), NOW) == me
        assert await credentials.resolve_api_token(hashed("token"), TOKEN_ENDS) is None
        assert await credentials.delete_api_token(me.id, mine.id) is True
        assert await credentials.resolve_api_token(hashed("token"), NOW) is None

    @credentials_test
    async def test_api_tokens_of_lists_the_owners_oldest_first(
        self, credentials: Credentials, store: Store
    ) -> None:
        me = await self.user(store)
        you = await self.user(store, "you")
        first = token(me, "first")
        second = token(me, "second", created_at=NOW + SECOND)
        for each in (second, token(you, "yours"), first):
            await credentials.add_api_token(each)
        assert await credentials.api_tokens_of(me.id) == [first, second]

    @credentials_test
    async def test_a_token_is_not_deleted_by_another_user(
        self, credentials: Credentials, store: Store
    ) -> None:
        me = await self.user(store)
        you = await self.user(store, "you")
        mine = token(me)
        await credentials.add_api_token(mine)
        assert await credentials.delete_api_token(you.id, mine.id) is False
        assert await credentials.resolve_api_token(hashed("token"), NOW) == me
