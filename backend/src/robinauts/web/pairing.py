# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Linking a phone (``docs/architecture/device-pairing.md``): the browser mints a pairing and
shows its code as a QR, a phone claims it and shows a number, the person types the number into
the browser, and the claim's secret becomes the device's. Over the credentials port, beside
``sign_in.py``, and with the same discipline: every call is given ``now``.

Three secrets take part, and none is stored: the code, the claim secret and the device secret,
which is the claim secret promoted. The database holds their SHA-256. The number is six digits,
stored as it is, since a hash of six digits hides nothing.

The QR is drawn here, as SVG, from the module matrix ``qrcodegen`` computes: one path of the
dark modules, a quiet zone of four, no script and no external reference.
"""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from urllib.parse import urlencode

from qrcodegen import QrCode

from robinauts.controller.contract.domain import (
    PAIRING_WRONG_TRIES,
    Approval,
    DeviceDescription,
    DeviceSession,
    Pairing,
    PairingState,
    User,
)
from robinauts.controller.contract.ports import Credentials
from robinauts.web.sign_in import SecretKind, random_secret, secret_hash, secret_of_kind

PAIRING_LIFE = timedelta(minutes=2)
"""From the mint, and again from the claim, so there is time to type the number."""
PAIRINGS_PER_USER = 3
WRONG_TRIES = PAIRING_WRONG_TRIES
FRESH_MINUTES = 10
"""How young a browser session must be to mint a credential: a pairing or an API token."""
NUMBER_DIGITS = 6
LINK_VERSION = 1
QUIET_MODULES = 4


class PairingErrorCode(StrEnum):
    """Why a pairing step was refused; the shell answers each with its status."""

    NO_DEVICES = "no_devices"
    BUSY = "busy"
    NOT_FOUND = "not_found"
    NOT_CLAIMED = "not_claimed"
    WRONG_NUMBER = "wrong_number"


class PairingError(Exception):
    def __init__(self, code: PairingErrorCode, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class Minted:
    """A pairing just minted, and its QR: the only form the code leaves the server in."""

    pairing: Pairing
    qr_svg: str


@dataclass(frozen=True, slots=True)
class Claimed:
    """What the phone is answered: the claim secret, inert until approved, and the number."""

    secret: str
    number: str
    expires_at: datetime


def is_fresh(signed_in_at: datetime, now: datetime) -> bool:
    """Whether a browser session signed in recently enough to mint a credential: its own age,
    whatever the provider did."""
    return timedelta(0) <= now - signed_in_at < timedelta(minutes=FRESH_MINUTES)


def match_number() -> str:
    """Six digits from a random source, zero-padded."""
    return f"{secrets.randbelow(10**NUMBER_DIGITS):0{NUMBER_DIGITS}d}"


def typed_number(typed: str) -> str | None:
    """The number as the person typed it, with or without the space in the middle; ``None``
    when it is not six digits."""
    digits = typed.replace(" ", "")
    if len(digits) == NUMBER_DIGITS and digits.isascii() and digits.isdigit():
        return digits
    return None


def link_url(origin: str, code: str) -> str:
    """What the QR says: a URL of the deployment, the code in its fragment, which a browser
    does not send and no server logs."""
    return f"{origin}/#/link?{urlencode({'v': LINK_VERSION, 'c': code})}"


def qr_svg(text: str) -> str:
    """``text`` as a QR code in SVG: one path of the dark modules, a quiet zone of four."""
    qr = QrCode.encode_text(text, QrCode.Ecc.MEDIUM)
    size = qr.get_size()
    full = size + 2 * QUIET_MODULES
    dark = "".join(
        f"M{x + QUIET_MODULES} {y + QUIET_MODULES}h1v1h-1z"
        for y in range(size)
        for x in range(size)
        if qr.get_module(x, y)
    )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {full} {full}"'
        ' shape-rendering="crispEdges">'
        f'<rect width="{full}" height="{full}" fill="#fff"/>'
        f'<path d="{dark}" fill="#000"/>'
        "</svg>"
    )


class Pairings:
    """The pairing flow. ``device_days`` is how long a linked device lives; 0 refuses to mint
    or approve."""

    def __init__(self, credentials: Credentials, *, device_days: float) -> None:
        self._credentials = credentials
        self._device_life = timedelta(days=device_days)

    @property
    def enabled(self) -> bool:
        return self._device_life > timedelta(0)

    async def mint(self, user: User, *, origin: str, now: datetime) -> Minted:
        """A waiting pairing of ``user``, and its QR, which names ``origin``."""
        self._check_enabled()
        code = random_secret()
        pairing = Pairing(
            uuid.uuid4(), user.id, secret_hash(code), PairingState.WAITING, now, now + PAIRING_LIFE
        )
        if not await self._credentials.add_pairing(pairing, now, at_most=PAIRINGS_PER_USER):
            raise PairingError(
                PairingErrorCode.BUSY,
                f"{PAIRINGS_PER_USER} codes are waiting already: use one, or let them expire",
            )
        return Minted(pairing, qr_svg(link_url(origin, code)))

    async def claim(self, code: str, device: DeviceDescription, now: datetime) -> Claimed:
        """The phone's claim. An unknown, spent or expired code is the same refusal."""
        secret = secret_of_kind(SecretKind.DEVICE)
        number = match_number()
        expires_at = now + PAIRING_LIFE
        claimed = await self._credentials.claim_pairing(
            secret_hash(code), secret_hash(secret), number, device, now, expires_at
        )
        if claimed is None:
            raise PairingError(PairingErrorCode.NOT_FOUND, "the code is unknown, used or expired")
        return Claimed(secret, number, expires_at)

    async def pairing_of(self, user: User, pairing_id: uuid.UUID, now: datetime) -> Pairing:
        """The owner's pairing, for the browser's poll; another's, or one gone or expired, is
        not found."""
        pairing = await self._credentials.pairing_of(user.id, pairing_id)
        if pairing is None or pairing.expires_at <= now:
            raise _no_such_pairing()
        return pairing

    async def approve(
        self, user: User, pairing_id: uuid.UUID, typed: str, now: datetime
    ) -> DeviceSession:
        """Link the phone that claimed the pairing, if ``typed`` is the number it shows."""
        self._check_enabled()
        number = typed_number(typed)
        outcome, device = await self._credentials.approve_pairing(
            user.id,
            pairing_id,
            # Not six digits is a wrong number too, and is counted as one.
            number or "-",
            device_id=uuid.uuid4(),
            device_expires_at=now + self._device_life,
            now=now,
        )
        match outcome:
            case Approval.APPROVED:
                assert device is not None
                return device
            case Approval.WRONG_NUMBER:
                raise PairingError(
                    PairingErrorCode.WRONG_NUMBER, "that is not the number the phone shows"
                )
            case Approval.NOT_CLAIMED:
                raise PairingError(
                    PairingErrorCode.NOT_CLAIMED, "no phone is waiting on this code for a number"
                )
        raise _no_such_pairing()

    async def refuse(self, user: User, pairing_id: uuid.UUID) -> None:
        """**No**, or a code cancelled: the pairing goes; a device it linked stays."""
        if not await self._credentials.delete_pairing(user.id, pairing_id):
            raise _no_such_pairing()

    async def poll(self, secret: str, now: datetime) -> PairingState | None:
        """The state of the pairing a claim secret belongs to, for the phone's poll; ``None``
        once it is approved, refused or expired."""
        return await self._credentials.resolve_claim(secret_hash(secret), now)

    def _check_enabled(self) -> None:
        if not self.enabled:
            raise PairingError(
                PairingErrorCode.NO_DEVICES, "this deployment links no devices: device_days is 0"
            )


def _no_such_pairing() -> PairingError:
    return PairingError(PairingErrorCode.NOT_FOUND, "you have no such pairing waiting")
