# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Linking a phone over the web app on the in-process credentials
(``docs/architecture/device-pairing.md``): the browser, with its session cookie, mints a code
and approves the number; a pretend phone, httpx with a bearer and no cookie, claims the code,
polls ``/auth/session`` and, once in, reads the API until the browser removes it. And the
refusals: No, three wrong numbers, an expired code, a second claim, the cap, a deployment with
no devices, and a bearer that tries to mint or remove. No provider is asked: the browser's
session is added to the credentials directly, and the code is read where it is minted."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from echo_controller import CONFIG

from robinauts.cli.composition import Composed, compose
from robinauts.controller.contract.domain import (
    DeviceDescription,
    DeviceSession,
    Identity,
    User,
    UserSession,
)
from robinauts.shared_domain.domain import StorageConfig, StorageKind
from robinauts.web import pairing
from robinauts.web.cookies import Cookies
from robinauts.web.sign_in import (
    AllowEntry,
    Matcher,
    ProviderConfig,
    SignInConfig,
    random_secret,
    secret_hash,
)
from util.aio import asyncio_test
from util.app import running

PUBLIC_URL = "https://robinauts.example.com"
OKTA = ProviderConfig(
    id="okta",
    title="Okta",
    issuer="https://example.okta.com/oauth2/default",
    client_id="robinauts",
    client_secret_env="ROBINAUTS_OKTA_SECRET",
)
SIGN_IN = SignInConfig(
    public_url=PUBLIC_URL,
    providers={"okta": OKTA},
    allow=(AllowEntry("okta", Matcher.GROUP, "robinauts-users"),),
)
ADA = Identity(
    "okta", "ada", name="Ada Lovelace", email="ada@example.com", groups=("robinauts-users",)
)
PIXEL = {"name": "Pixel 8", "platform": "android", "app_version": "1.0.0"}
TURN = {"agent_id": "echo", "text": "hello"}


@dataclass
class Rig:
    http: httpx.AsyncClient
    composed: Composed
    ada: User
    browser: dict[str, str]
    """Ada's session cookie and the ``Origin`` a write with it needs."""
    codes: list[str]
    """Every pairing code minted, as the QR carries it."""

    async def session(self, age: timedelta = timedelta(0)) -> dict[str, str]:
        """The headers of another browser of Ada's, signed in ``age`` ago."""
        secret = random_secret()
        at = datetime.now(UTC) - age
        await self.composed.credentials.add_user_session(
            UserSession(
                uuid.uuid4(), self.ada.id, secret_hash(secret), at, at + timedelta(hours=12)
            )
        )
        return {"cookie": f"{Cookies(PUBLIC_URL).session}={secret}", "origin": PUBLIC_URL}

    async def mint(self) -> tuple[str, str]:
        """A code minted in Ada's browser: the pairing's id, and the code."""
        minted = await self.http.post("/auth/pairings", headers=self.browser)
        assert minted.status_code == 201, minted.text
        return minted.json()["id"], self.codes[-1]

    async def claim(self, code: str, device: dict[str, object] = PIXEL) -> httpx.Response:
        return await self.http.post("/auth/pairings/claim", json={"code": code, "device": device})


@asynccontextmanager
async def rig(
    monkeypatch: pytest.MonkeyPatch, sign_in: SignInConfig = SIGN_IN
) -> AsyncIterator[Rig]:
    codes: list[str] = []

    def recorded() -> str:
        codes.append(random_secret())
        return codes[-1]

    monkeypatch.setattr(pairing, "random_secret", recorded)
    composed = compose(CONFIG, storage=StorageConfig(StorageKind.IN_PROCESS), secret_for={}.get)
    async with running(composed, sign_in=sign_in, secret_for={}.get) as app:
        ada = await composed.controller.ensure_user(ADA)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=PUBLIC_URL) as http:
            made = Rig(http, composed, ada, {}, codes)
            made.browser = await made.session()
            yield made


def bearer(secret: str) -> dict[str, str]:
    return {"authorization": f"Bearer {secret}"}


@asyncio_test
async def test_a_phone_is_linked_by_the_number_it_shows_and_is_out_once_removed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with rig(monkeypatch) as at:
        http, browser = at.http, at.browser
        minted = await http.post("/auth/pairings", headers=browser)
        assert minted.status_code == 201
        answer = minted.json()
        assert answer.keys() == {"id", "expires_at", "qr_svg"}
        (code,) = at.codes
        assert code not in minted.text
        assert answer["qr_svg"].startswith("<svg ")
        pairing_id = answer["id"]
        lived = datetime.fromisoformat(answer["expires_at"]) - datetime.now(UTC)
        assert timedelta(minutes=1) < lived <= timedelta(minutes=2)

        polled = (await http.get(f"/auth/pairings/{pairing_id}", headers=browser)).json()
        assert polled["state"] == "waiting"
        assert polled["device"] is None

        # The phone presents the code, with no credential, and shows the number.
        claimed = await at.claim(code)
        assert claimed.status_code == 200
        phone = claimed.json()
        assert phone.keys() == {"secret", "number", "expires_at"}
        assert phone["secret"].startswith("rbd_")
        assert len(phone["number"]) == 6
        assert phone["number"].isdigit()
        key = bearer(phone["secret"])

        polled = (await http.get(f"/auth/pairings/{pairing_id}", headers=browser)).json()
        assert polled["state"] == "claimed"
        assert polled["device"] == PIXEL
        waiting = (await http.get("/auth/session", headers=key)).json()
        assert waiting["user"] is None
        assert waiting["pairing"] == {"state": "claimed"}
        assert (await http.get("/api/conversations", headers=key)).status_code == 401

        # The person types the number, with the space the phone shows it with.
        number = phone["number"]
        approved = await http.post(
            f"/auth/pairings/{pairing_id}/approve",
            json={"number": f"{number[:3]} {number[3:]}"},
            headers=browser,
        )
        assert approved.status_code == 200
        device = approved.json()
        assert device["name"] == "Pixel 8"
        assert device["platform"] == "android"
        lived = datetime.fromisoformat(device["expires_at"]) - datetime.now(UTC)
        assert timedelta(days=29) < lived <= timedelta(days=30)
        polled = (await http.get(f"/auth/pairings/{pairing_id}", headers=browser)).json()
        assert polled["state"] == "approved"

        # The phone is in: its session names Ada and the device, and it reads her chats.
        session = (await http.get("/auth/session", headers=key)).json()
        assert session["user"]["name"] == "Ada Lovelace"
        assert session["device"] == device
        assert session["pairing"] is None
        started = await http.post("/api/turns", json=TURN, headers=browser)
        conversation_id = started.headers["x-robinauts-conversation-id"]
        mine = (await http.get("/api/conversations", headers=key)).json()["items"]
        assert [c["id"] for c in mine] == [conversation_id]
        listed = (await http.get("/auth/devices", headers=key)).json()["items"]
        assert listed == [device]

        # The browser removes it, and the phone is out.
        removed = await http.delete(f"/auth/devices/{device['id']}", headers=browser)
        assert removed.status_code == 204
        refused = await http.get("/api/conversations", headers=key)
        assert refused.status_code == 401
        assert refused.json()["error"] == "Unauthorized"
        out = (await http.get("/auth/session", headers=key)).json()
        assert out["user"] is None
        assert out["pairing"] is None
        assert (await http.get("/auth/devices", headers=browser)).json() == {"items": []}
        again = await http.delete(f"/auth/devices/{device['id']}", headers=browser)
        assert again.status_code == 404


@asyncio_test
async def test_no_ends_the_pairing_and_the_claim_never_resolves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with rig(monkeypatch) as at:
        pairing_id, code = await at.mint()
        key = bearer((await at.claim(code)).json()["secret"])
        refused = await at.http.delete(f"/auth/pairings/{pairing_id}", headers=at.browser)
        assert refused.status_code == 204
        session = (await at.http.get("/auth/session", headers=key)).json()
        assert session["user"] is None
        assert session["pairing"] is None
        gone = await at.http.get(f"/auth/pairings/{pairing_id}", headers=at.browser)
        assert gone.status_code == 404
        assert (
            await at.http.delete(f"/auth/pairings/{pairing_id}", headers=at.browser)
        ).status_code == 404


@asyncio_test
async def test_two_wrong_numbers_are_refused_and_the_third_ends_the_pairing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with rig(monkeypatch) as at:
        pairing_id, code = await at.mint()
        phone = (await at.claim(code)).json()
        wrong = "000000" if phone["number"] != "000000" else "111111"
        approve = f"/auth/pairings/{pairing_id}/approve"
        for _ in range(2):
            refused = await at.http.post(approve, json={"number": wrong}, headers=at.browser)
            assert refused.status_code == 403
            assert refused.json()["error"] == "WrongNumber"
        # Not six digits is not a try: the body is refused before anything is counted.
        malformed = await at.http.post(approve, json={"number": "12345"}, headers=at.browser)
        assert malformed.status_code == 422
        third = await at.http.post(approve, json={"number": wrong}, headers=at.browser)
        assert third.status_code == 404
        late = await at.http.post(approve, json={"number": phone["number"]}, headers=at.browser)
        assert late.status_code == 404
        session = (await at.http.get("/auth/session", headers=bearer(phone["secret"]))).json()
        assert session == {**session, "user": None, "pairing": None}


@asyncio_test
async def test_a_code_is_claimed_once_and_not_once_expired_and_approved_only_once_claimed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with rig(monkeypatch) as at:
        pairing_id, code = await at.mint()
        approve = f"/auth/pairings/{pairing_id}/approve"
        early = await at.http.post(approve, json={"number": "123456"}, headers=at.browser)
        assert early.status_code == 409
        assert early.json()["error"] == "Conflict"
        assert (await at.claim(code)).status_code == 200
        second = await at.claim(code)
        unknown = await at.claim(random_secret())
        assert second.status_code == unknown.status_code == 404
        assert second.json() == unknown.json()

        # A code that waited past its two minutes is the same 404.
        _, stale = await at.mint()
        credentials = at.composed.credentials
        for kept in list(credentials._pairings.values()):
            if kept.code_hash == secret_hash(stale):
                past = datetime.now(UTC) - timedelta(seconds=1)
                credentials._pairings[kept.id] = replace(kept, expires_at=past)
        expired = await at.claim(stale)
        assert expired.status_code == 404
        assert expired.json() == unknown.json()


@asyncio_test
async def test_the_claim_bounds_what_a_phone_says_of_itself(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with rig(monkeypatch) as at:
        _, code = await at.mint()
        for device in (
            {**PIXEL, "name": "x" * 65},
            {**PIXEL, "name": ""},
            {**PIXEL, "name": "Pixel\n8"},
            {**PIXEL, "platform": "fridge"},
            {**PIXEL, "app_version": "1" * 33},
            {"platform": "android"},
        ):
            refused = await at.claim(code, device)
            assert refused.status_code == 422, device
        assert (await at.claim(code, {"name": "x" * 64, "platform": "other"})).status_code == 200


@asyncio_test
async def test_three_codes_wait_at_once_and_a_fourth_is_busy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with rig(monkeypatch) as at:
        for _ in range(3):
            await at.mint()
        busy = await at.http.post("/auth/pairings", headers=at.browser)
        assert busy.status_code == 429
        assert busy.json()["error"] == "Busy"


@asyncio_test
async def test_a_deployment_with_no_devices_mints_no_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with rig(monkeypatch, replace(SIGN_IN, device_days=0)) as at:
        refused = await at.http.post("/auth/pairings", headers=at.browser)
        assert refused.status_code == 403
        assert refused.json()["error"] == "Forbidden"
        assert (await at.http.get("/auth/devices", headers=at.browser)).json() == {"items": []}


@asyncio_test
async def test_minting_wants_a_fresh_session_and_removing_does_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with rig(monkeypatch) as at:
        stale = await at.session(age=timedelta(minutes=11))
        refused = await at.http.post("/auth/pairings", headers=stale)
        assert refused.status_code == 403
        assert refused.json()["error"] == "FreshSignInRequired"
        device = await linked(at)
        removed = await at.http.delete(f"/auth/devices/{device.id}", headers=stale)
        assert removed.status_code == 204


async def linked(at: Rig, secret: str = "rbd_linked") -> DeviceSession:
    """A device of Ada's, added to the credentials directly."""
    now = datetime.now(UTC)
    device = DeviceSession(
        uuid.uuid4(),
        at.ada.id,
        DeviceDescription("Pixel 8", "android"),
        secret_hash(secret),
        now,
        now,
        now + timedelta(days=30),
    )
    await at.composed.credentials.add_device_session(device)
    return device


@asyncio_test
async def test_a_bearer_mints_nothing_and_removes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with rig(monkeypatch) as at:
        device = await linked(at)
        pairing_id, code = await at.mint()
        key = bearer("rbd_linked")
        for method, path, body in (
            ("POST", "/auth/pairings", None),
            ("GET", f"/auth/pairings/{pairing_id}", None),
            ("POST", f"/auth/pairings/{pairing_id}/approve", {"number": "123456"}),
            ("DELETE", f"/auth/pairings/{pairing_id}", None),
            ("DELETE", f"/auth/devices/{device.id}", None),
            ("POST", "/auth/tokens", {"name": "from the phone"}),
            ("DELETE", f"/auth/tokens/{uuid.uuid4()}", None),
        ):
            refused = await at.http.request(method, path, json=body, headers=key)
            assert refused.status_code == 403, path
            assert refused.json()["error"] == "Forbidden", path
        assert (await at.http.get("/auth/devices", headers=key)).status_code == 200
        assert (await at.http.get("/auth/tokens", headers=key)).status_code == 200
        assert (await at.claim(code)).status_code == 200


@asyncio_test
async def test_a_device_signs_itself_out_and_a_token_does_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with rig(monkeypatch) as at:
        await linked(at)
        minted = await at.http.post("/auth/tokens", json={"name": "ci"}, headers=at.browser)
        token = bearer(minted.json()["secret"])
        key = bearer("rbd_linked")
        out = await at.http.post("/auth/logout", headers=key)
        assert out.status_code == 204
        assert "set-cookie" not in out.headers
        assert (await at.http.get("/api/conversations", headers=key)).status_code == 401
        assert (await at.http.post("/auth/logout", headers=token)).status_code == 204
        assert (await at.http.get("/api/conversations", headers=token)).status_code == 200
        assert (await at.http.get("/api/conversations", headers=at.browser)).status_code == 200


@asyncio_test
async def test_a_user_out_of_the_allow_list_loses_every_credential_on_use(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with rig(monkeypatch) as at:
        await linked(at)
        minted = await at.http.post("/auth/tokens", json={"name": "ci"}, headers=at.browser)
        token = bearer(minted.json()["secret"])
        # Her last sign-in, refreshed: no longer in the group the allow list names.
        await at.composed.controller.ensure_user(replace(ADA, groups=()))
        for key in (bearer("rbd_linked"), token, at.browser):
            refused = await at.http.get("/api/conversations", headers=key)
            assert refused.status_code == 401
        # Put back in the group, she has nothing left that works.
        await at.composed.controller.ensure_user(ADA)
        for key in (bearer("rbd_linked"), token, at.browser):
            assert (await at.http.get("/api/conversations", headers=key)).status_code == 401
