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

from robinauts.controller.contract.domain import (
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
from robinauts.controller.ports.store import Store

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
SECOND = timedelta(seconds=1)
LOGIN_LIFE = timedelta(minutes=10)
LOGIN_ENDS = NOW + LOGIN_LIFE
SESSION_ENDS = NOW + timedelta(hours=12)
TOKEN_ENDS = NOW + timedelta(days=90)
PAIRING_LIFE = timedelta(minutes=2)
PAIRING_ENDS = NOW + PAIRING_LIFE
CLAIMED_ENDS = NOW + 2 * PAIRING_LIFE
DEVICE_ENDS = NOW + timedelta(days=30)
PIXEL = DeviceDescription("Pixel 8", "android", "1.0.0")


def hashed(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def session(user: User, secret: str = "cookie") -> UserSession:
    return UserSession(uuid.uuid4(), user.id, hashed(secret), NOW, SESSION_ENDS)


def pending(state: str = "state", expires_at: datetime = LOGIN_ENDS) -> PendingLogin:
    return PendingLogin(hashed(state), "okta", "nonce", "verifier", "/#/chat", NOW, expires_at)


def token(user: User, secret: str = "token", created_at: datetime = NOW) -> ApiToken:
    return ApiToken(uuid.uuid4(), user.id, "ci", hashed(secret), created_at, TOKEN_ENDS)


def pairing(user: User, code: str = "code", expires_at: datetime = PAIRING_ENDS) -> Pairing:
    return Pairing(uuid.uuid4(), user.id, hashed(code), PairingState.WAITING, NOW, expires_at)


def device(user: User, secret: str = "rbd_device", created_at: datetime = NOW) -> DeviceSession:
    return DeviceSession(
        uuid.uuid4(), user.id, PIXEL, hashed(secret), created_at, created_at, DEVICE_ENDS
    )


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
        return await store.add_or_refresh_user(User(uuid.uuid4(), "okta", subject, created_at=NOW))

    # --- user sessions ------------------------------------------------------

    @credentials_test
    async def test_a_session_resolves_to_its_user_until_now_passes_its_expiry(
        self, credentials: Credentials, store: Store
    ) -> None:
        me = await self.user(store)
        mine = session(me)
        await credentials.add_user_session(mine)
        assert await credentials.resolve_user_session(hashed("cookie"), NOW) == (me, mine)
        assert await credentials.resolve_user_session(hashed("cookie"), SESSION_ENDS - SECOND) == (
            me,
            mine,
        )
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

    # --- pairings -----------------------------------------------------------

    async def claimed(
        self, credentials: Credentials, user: User, code: str = "code", number: str = "482913"
    ) -> Pairing:
        mint = pairing(user, code)
        assert await credentials.add_pairing(mint, NOW, at_most=3) is True
        claimed = await credentials.claim_pairing(
            hashed(code), hashed(f"rbd_{code}"), number, PIXEL, NOW, CLAIMED_ENDS
        )
        assert claimed is not None
        return claimed

    @credentials_test
    async def test_a_pairing_is_its_owners_and_waits_until_claimed(
        self, credentials: Credentials, store: Store
    ) -> None:
        me = await self.user(store)
        you = await self.user(store, "you")
        mint = pairing(me)
        assert await credentials.add_pairing(mint, NOW, at_most=3) is True
        assert await credentials.pairing_of(me.id, mint.id) == mint
        assert await credentials.pairing_of(you.id, mint.id) is None
        assert await credentials.delete_pairing(you.id, mint.id) is False
        assert await credentials.delete_pairing(me.id, mint.id) is True
        assert await credentials.pairing_of(me.id, mint.id) is None

    @credentials_test
    async def test_adding_a_pairing_deletes_the_users_expired_ones_and_keeps_the_cap(
        self, credentials: Credentials, store: Store
    ) -> None:
        me = await self.user(store)
        you = await self.user(store, "you")
        old = pairing(me, "old")
        yours = pairing(you, "yours")
        for each in (old, yours):
            assert await credentials.add_pairing(each, NOW, at_most=2) is True
        assert await credentials.add_pairing(pairing(me, "second"), NOW, at_most=2) is True
        assert await credentials.add_pairing(pairing(me, "third"), NOW, at_most=2) is False
        # Once the first two expire, they make room, and only the user's own are deleted.
        later = pairing(me, "later", expires_at=PAIRING_ENDS + PAIRING_LIFE)
        assert await credentials.add_pairing(later, PAIRING_ENDS, at_most=2) is True
        assert await credentials.pairing_of(me.id, old.id) is None
        assert await credentials.pairing_of(you.id, yours.id) == yours
        assert await credentials.pairing_of(me.id, later.id) == later

    @credentials_test
    async def test_a_claim_records_the_device_and_the_number_and_spends_the_code(
        self, credentials: Credentials, store: Store
    ) -> None:
        me = await self.user(store)
        claimed = await self.claimed(credentials, me)
        assert claimed.state is PairingState.CLAIMED
        assert claimed.claim_hash == hashed("rbd_code")
        assert claimed.match_number == "482913"
        assert claimed.device == PIXEL
        assert claimed.expires_at == CLAIMED_ENDS
        assert claimed.wrong_tries == 0
        assert await credentials.pairing_of(me.id, claimed.id) == claimed
        again = await credentials.claim_pairing(
            hashed("code"), hashed("rbd_again"), "111111", PIXEL, NOW, CLAIMED_ENDS
        )
        assert again is None
        assert await credentials.resolve_claim(hashed("rbd_code"), NOW) is PairingState.CLAIMED
        assert await credentials.resolve_claim(hashed("rbd_code"), CLAIMED_ENDS) is None
        assert await credentials.resolve_claim(hashed("rbd_again"), NOW) is None

    @credentials_test
    async def test_an_expired_or_unknown_code_is_not_claimed(
        self, credentials: Credentials, store: Store
    ) -> None:
        me = await self.user(store)
        await credentials.add_pairing(pairing(me), NOW, at_most=3)
        for code, at in (("code", PAIRING_ENDS), ("unknown", NOW)):
            claimed = await credentials.claim_pairing(
                hashed(code), hashed("rbd_claim"), "482913", PIXEL, at, CLAIMED_ENDS
            )
            assert claimed is None, code

    @credentials_test
    async def test_of_two_claims_of_one_code_at_once_one_wins(
        self, credentials: Credentials, store: Store
    ) -> None:
        me = await self.user(store)
        await credentials.add_pairing(pairing(me), NOW, at_most=3)
        claims = await asyncio.gather(
            *(
                credentials.claim_pairing(
                    hashed("code"), hashed(f"rbd_{n}"), "482913", PIXEL, NOW, CLAIMED_ENDS
                )
                for n in range(2)
            )
        )
        assert len([c for c in claims if c is not None]) == 1

    @credentials_test
    async def test_the_right_number_moves_the_claim_hash_to_a_device_session(
        self, credentials: Credentials, store: Store
    ) -> None:
        me = await self.user(store)
        claimed = await self.claimed(credentials, me)
        device_id = uuid.uuid4()
        outcome, linked = await credentials.approve_pairing(
            me.id, claimed.id, "482913", device_id=device_id, device_expires_at=DEVICE_ENDS, now=NOW
        )
        assert outcome is Approval.APPROVED
        assert linked == DeviceSession(
            device_id, me.id, PIXEL, hashed("rbd_code"), NOW, NOW, DEVICE_ENDS
        )
        approved = await credentials.pairing_of(me.id, claimed.id)
        assert approved is not None
        assert approved.state is PairingState.APPROVED
        assert approved.claim_hash is None
        # Moved, not copied: the claim no longer resolves, the device does.
        assert await credentials.resolve_claim(hashed("rbd_code"), NOW) is None
        assert await credentials.resolve_device_session(hashed("rbd_code"), NOW) == (me, linked)
        assert await credentials.device_sessions_of(me.id) == [linked]
        # Approved once: a second approve has nothing to decide.
        again = await credentials.approve_pairing(
            me.id,
            claimed.id,
            "482913",
            device_id=uuid.uuid4(),
            device_expires_at=DEVICE_ENDS,
            now=NOW,
        )
        assert again == (Approval.NOT_CLAIMED, None)

    @credentials_test
    async def test_a_waiting_an_expired_or_anothers_pairing_is_not_approved(
        self, credentials: Credentials, store: Store
    ) -> None:
        me = await self.user(store)
        you = await self.user(store, "you")
        waiting = pairing(me, "waiting")
        await credentials.add_pairing(waiting, NOW, at_most=3)
        claimed = await self.claimed(credentials, me)

        async def approve(user: User, pairing_id: uuid.UUID, at: datetime) -> Approval:
            outcome, linked = await credentials.approve_pairing(
                user.id,
                pairing_id,
                "482913",
                device_id=uuid.uuid4(),
                device_expires_at=DEVICE_ENDS,
                now=at,
            )
            assert linked is None
            return outcome

        assert await approve(me, waiting.id, NOW) is Approval.NOT_CLAIMED
        assert await approve(you, claimed.id, NOW) is Approval.GONE
        assert await approve(me, claimed.id, CLAIMED_ENDS) is Approval.GONE
        assert await approve(me, uuid.uuid4(), NOW) is Approval.GONE
        assert await credentials.device_sessions_of(me.id) == []

    @credentials_test
    async def test_a_wrong_number_counts_and_the_last_wrong_try_deletes_the_pairing(
        self, credentials: Credentials, store: Store
    ) -> None:
        me = await self.user(store)
        claimed = await self.claimed(credentials, me)

        async def approve(number: str) -> Approval:
            outcome, _ = await credentials.approve_pairing(
                me.id,
                claimed.id,
                number,
                device_id=uuid.uuid4(),
                device_expires_at=DEVICE_ENDS,
                now=NOW,
            )
            return outcome

        for tries in range(1, PAIRING_WRONG_TRIES):
            assert await approve("000000") is Approval.WRONG_NUMBER
            counted = await credentials.pairing_of(me.id, claimed.id)
            assert counted is not None
            assert counted.wrong_tries == tries
        assert await approve("000000") is Approval.GONE
        assert await credentials.pairing_of(me.id, claimed.id) is None
        assert await approve("482913") is Approval.GONE
        assert await credentials.resolve_claim(hashed("rbd_code"), NOW) is None

    @credentials_test
    async def test_of_two_approves_at_once_one_links_a_device(
        self, credentials: Credentials, store: Store
    ) -> None:
        me = await self.user(store)
        claimed = await self.claimed(credentials, me)
        outcomes = await asyncio.gather(
            *(
                credentials.approve_pairing(
                    me.id,
                    claimed.id,
                    "482913",
                    device_id=uuid.uuid4(),
                    device_expires_at=DEVICE_ENDS,
                    now=NOW,
                )
                for _ in range(2)
            )
        )
        assert sorted(o for o, _ in outcomes) == sorted([Approval.APPROVED, Approval.NOT_CLAIMED])
        assert len(await credentials.device_sessions_of(me.id)) == 1

    @credentials_test
    async def test_deleting_an_approved_pairing_keeps_its_device(
        self, credentials: Credentials, store: Store
    ) -> None:
        me = await self.user(store)
        claimed = await self.claimed(credentials, me)
        _, linked = await credentials.approve_pairing(
            me.id,
            claimed.id,
            "482913",
            device_id=uuid.uuid4(),
            device_expires_at=DEVICE_ENDS,
            now=NOW,
        )
        assert await credentials.delete_pairing(me.id, claimed.id) is True
        assert await credentials.pairing_of(me.id, claimed.id) is None
        assert await credentials.device_sessions_of(me.id) == [linked]

    # --- device sessions ----------------------------------------------------

    @credentials_test
    async def test_a_device_resolves_until_deleted_and_not_past_its_expiry(
        self, credentials: Credentials, store: Store
    ) -> None:
        me = await self.user(store)
        you = await self.user(store, "you")
        mine = device(me)
        await credentials.add_device_session(mine)
        assert await credentials.resolve_device_session(hashed("rbd_device"), NOW) == (me, mine)
        assert await credentials.resolve_device_session(hashed("rbd_device"), DEVICE_ENDS) is None
        assert await credentials.resolve_device_session(hashed("rbd_other"), NOW) is None
        assert await credentials.delete_device_session(you.id, mine.id) is False
        assert await credentials.delete_device_session(me.id, mine.id) is True
        assert await credentials.resolve_device_session(hashed("rbd_device"), NOW) is None

    @credentials_test
    async def test_last_used_at_is_written_at_most_once_a_minute(
        self, credentials: Credentials, store: Store
    ) -> None:
        me = await self.user(store)
        await credentials.add_device_session(device(me))

        async def last_used(at: datetime) -> datetime:
            found = await credentials.resolve_device_session(hashed("rbd_device"), at)
            assert found is not None
            return found[1].last_used_at

        minute = timedelta(minutes=1)
        assert await last_used(NOW + minute - SECOND) == NOW
        assert await last_used(NOW + minute) == NOW + minute
        assert await last_used(NOW + 2 * minute - SECOND) == NOW + minute
        (listed,) = await credentials.device_sessions_of(me.id)
        assert listed.last_used_at == NOW + minute

    @credentials_test
    async def test_devices_are_listed_oldest_first_for_the_owner_and_for_the_operator(
        self, credentials: Credentials, store: Store
    ) -> None:
        me = await self.user(store)
        you = await self.user(store, "you")
        first = device(me, "rbd_first")
        second = device(me, "rbd_second", created_at=NOW + SECOND)
        yours = device(you, "rbd_yours", created_at=NOW + 2 * SECOND)
        for each in (second, yours, first):
            await credentials.add_device_session(each)
        assert await credentials.device_sessions_of(me.id) == [first, second]
        assert await credentials.all_device_sessions() == [
            (me, first),
            (me, second),
            (you, yours),
        ]

    @credentials_test
    async def test_the_operator_ends_one_device_a_users_devices_or_every_device(
        self, credentials: Credentials, store: Store
    ) -> None:
        me = await self.user(store)
        you = await self.user(store, "you")
        first = device(me, "rbd_first")
        second = device(me, "rbd_second", created_at=NOW + SECOND)
        yours = device(you, "rbd_yours")
        for each in (first, second, yours):
            await credentials.add_device_session(each)
        assert await credentials.delete_device_sessions(device_id=first.id) == [first]
        assert await credentials.delete_device_sessions(user_id=me.id) == [second]
        assert await credentials.delete_device_sessions(device_id=first.id) == []
        assert await credentials.delete_device_sessions(every=True) == [yours]
        assert await credentials.all_device_sessions() == []
        for none_or_two in ({}, {"user_id": me.id, "every": True}):
            try:
                await credentials.delete_device_sessions(**none_or_two)
            except ValueError:
                continue
            raise AssertionError(f"{none_or_two} was taken")

    @credentials_test
    async def test_users_are_found_by_email_whatever_its_case(
        self, credentials: Credentials, store: Store
    ) -> None:
        ada = await store.add_or_refresh_user(
            User(uuid.uuid4(), "okta", "ada", email="Ada@Example.com", created_at=NOW)
        )
        again = await store.add_or_refresh_user(
            User(uuid.uuid4(), "google", "ada", email="ada@example.com", created_at=NOW + SECOND)
        )
        await self.user(store, "nobody")
        assert await credentials.users_with_email("ADA@example.com") == [ada, again]
        assert await credentials.users_with_email("grace@example.com") == []

    # --- every credential of a person ---------------------------------------

    async def everything_of(self, credentials: Credentials, user: User, tag: str) -> None:
        await credentials.add_user_session(session(user, f"cookie-{tag}"))
        await credentials.add_api_token(token(user, f"token-{tag}"))
        await credentials.add_device_session(device(user, f"rbd_device-{tag}"))
        await credentials.add_pairing(pairing(user, f"code-{tag}"), NOW, at_most=3)

    async def holds(self, credentials: Credentials, user: User, tag: str) -> list[bool]:
        return [
            await credentials.resolve_user_session(hashed(f"cookie-{tag}"), NOW) is not None,
            await credentials.resolve_api_token(hashed(f"token-{tag}"), NOW) is not None,
            await credentials.resolve_device_session(hashed(f"rbd_device-{tag}"), NOW) is not None,
            await credentials.claim_pairing(
                hashed(f"code-{tag}"),
                hashed(f"rbd_claim-{tag}"),
                "482913",
                PIXEL,
                NOW,
                CLAIMED_ENDS,
            )
            is not None,
        ]

    @credentials_test
    async def test_every_credential_of_a_user_is_deleted_at_once(
        self, credentials: Credentials, store: Store
    ) -> None:
        me = await self.user(store)
        you = await self.user(store, "you")
        await self.everything_of(credentials, me, "mine")
        await self.everything_of(credentials, you, "yours")
        assert await credentials.delete_credentials_of(me.id) == 4
        assert await self.holds(credentials, me, "mine") == [False] * 4
        assert await self.holds(credentials, you, "yours") == [True] * 4

    @credentials_test
    async def test_every_credential_of_an_identity_is_deleted_at_once(
        self, credentials: Credentials, store: Store
    ) -> None:
        me = await self.user(store)
        you = await self.user(store, "you")
        await self.everything_of(credentials, me, "mine")
        await self.everything_of(credentials, you, "yours")
        assert await credentials.delete_credentials_of_identity("okta", "me") == 4
        assert await credentials.delete_credentials_of_identity("okta", "nobody") == 0
        assert await credentials.delete_credentials_of_identity("google", "you") == 0
        assert await self.holds(credentials, me, "mine") == [False] * 4
        assert await self.holds(credentials, you, "yours") == [True] * 4
