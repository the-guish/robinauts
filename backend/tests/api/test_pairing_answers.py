# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""What pairing answers carry, and nothing else (``docs/architecture/device-pairing.md``,
"Routes"): the mint answers the QR and never its code; a claim answers the claim secret, the
number and the expiry; a code that is unknown, spent or expired is one 404, the same for each.
The server is the local development mode's, whose one user mints; the claimed pairing is
planted in its database, since the code exists outside the picture nowhere else."""

from __future__ import annotations

import asyncio
import hashlib
import re
import uuid
from datetime import UTC, datetime, timedelta

import asyncpg
import httpx

from util.controller_db import requires_postgres

pytestmark = requires_postgres

PIXEL = {"name": "Pixel 8", "platform": "android", "app_version": "1.0.0"}


def hashed(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def plant(database_url: str, user_id: str, code: str, *, expires_in: timedelta) -> None:
    """A waiting pairing of that user, under the hash of a code this test knows."""

    async def go() -> None:
        connection = await asyncpg.connect(database_url)
        try:
            now = datetime.now(UTC)
            await connection.execute(
                "INSERT INTO pairings (id, user_id, code_hash, state, wrong_tries, created_at,"
                " expires_at) VALUES ($1, $2, $3, 'waiting', 0, $4, $5)",
                uuid.uuid4(),
                uuid.UUID(user_id),
                hashed(code),
                now,
                now + expires_in,
            )
        finally:
            await connection.close()

    asyncio.run(go())


def codes_of(database_url: str, pairing_id: str) -> str:
    async def go() -> str:
        connection = await asyncpg.connect(database_url)
        try:
            return await connection.fetchval(
                "SELECT code_hash FROM pairings WHERE id = $1", uuid.UUID(pairing_id)
            )
        finally:
            await connection.close()

    return asyncio.run(go())


def test_the_mint_answers_the_picture_and_never_the_code(
    api: httpx.Client, database_url: str
) -> None:
    minted = api.post("/auth/pairings")
    assert minted.status_code == 201
    answer = minted.json()
    assert answer.keys() == {"id", "expires_at", "qr_svg"}
    code_hash = codes_of(database_url, answer["id"])
    # Nothing in the answer that a code could be hashes to the pairing's code.
    candidates = re.findall(r"[A-Za-z0-9_-]{20,}", minted.text)
    assert all(hashed(c) != code_hash for c in candidates)
    assert "/#/link" not in minted.text
    api.delete(f"/auth/pairings/{answer['id']}")


def test_a_claim_answers_its_secret_number_and_expiry_and_one_404_otherwise(
    api: httpx.Client, database_url: str
) -> None:
    user_id = api.get("/auth/session").json()["user"]["id"]
    code, stale = uuid.uuid4().hex * 2, uuid.uuid4().hex * 2
    plant(database_url, user_id, code, expires_in=timedelta(minutes=2))
    plant(database_url, user_id, stale, expires_in=timedelta(seconds=-1))

    claimed = api.post("/auth/pairings/claim", json={"code": code, "device": PIXEL})
    assert claimed.status_code == 200
    answer = claimed.json()
    assert answer.keys() == {"secret", "number", "expires_at"}
    assert re.fullmatch(r"rbd_[A-Za-z0-9_-]{43}", answer["secret"])
    assert re.fullmatch(r"[0-9]{6}", answer["number"])
    assert code not in claimed.text

    refusals = [
        api.post("/auth/pairings/claim", json={"code": c, "device": PIXEL})
        for c in (code, stale, uuid.uuid4().hex * 2)
    ]
    assert [r.status_code for r in refusals] == [404, 404, 404]
    assert all(r.json() == refusals[0].json() for r in refusals)
    assert refusals[0].json().keys() == {"error", "detail"}
    assert all(code not in r.text and stale not in r.text for r in refusals)
