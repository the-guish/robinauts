# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The stand-in provider, tested as the thing the other suites lean on.

A stand-in that quietly stops standing in makes every test above it pass for
the wrong reason -- a "slow" provider that answers at once proves no timeout,
and an authorization server that burns a code before it has authenticated
anybody turns one refusal into a different one. So the two places where that
could happen are tested here, against the stand-in itself.

Marked ``io``: it opens sockets.
"""

from __future__ import annotations

import json
from collections.abc import Iterator

import httpx
import pytest

from aio import asyncio_test
from standin import Misbehaviour, StandInProvider, redirect_from

pytestmark = pytest.mark.io

REDIRECT_URI = "https://robinauts.example.com/auth/callback/okta"
VERIFIER = "the-pkce-verifier-nothing-may-ever-repeat"


@pytest.fixture
def stand_in() -> Iterator[StandInProvider]:
    with StandInProvider() as provider:
        yield provider


async def asking(stand_in: StandInProvider, path: str, timeout: float = 5.0) -> httpx.Response:
    """One GET, with a timeout of the test's choosing."""
    async with httpx.AsyncClient(trust_env=False, timeout=timeout) as client:
        return await client.get(f"{stand_in.issuer}{path}")


async def code_for(stand_in: StandInProvider) -> str:
    """Mint a code the way the browser does."""
    from robinauts.legacy.core import pkce_challenge

    location = await redirect_from(
        f"{stand_in.issuer}/authorize?response_type=code"
        f"&client_id={stand_in.client_id}"
        f"&redirect_uri={httpx.URL(REDIRECT_URI)}"
        f"&state=state-1&nonce=nonce-1"
        f"&code_challenge={pkce_challenge(VERIFIER)}&code_challenge_method=S256"
    )
    return dict(httpx.URL(location).params)["code"]


async def redeeming(stand_in: StandInProvider, code: str, *, secret: str) -> httpx.Response:
    """Post the grant with ``secret`` as the client secret, the simple way."""
    async with httpx.AsyncClient(trust_env=False, timeout=5.0) as client:
        return await client.post(
            f"{stand_in.issuer}/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT_URI,
                "code_verifier": VERIFIER,
                "client_id": stand_in.client_id,
                "client_secret": secret,
            },
        )


@asyncio_test
async def test_a_hold_really_holds(stand_in: StandInProvider) -> None:
    stand_in.discovery_misbehaves(Misbehaviour.slow())

    try:
        with pytest.raises(httpx.TimeoutException):
            await asking(stand_in, "/.well-known/openid-configuration", timeout=0.25)
    finally:
        stand_in.release()


@asyncio_test
async def test_releasing_once_does_not_release_what_is_scripted_afterwards(
    stand_in: StandInProvider,
) -> None:
    # A single sticky flag would make this second hold answer at once, and
    # every later timeout test in the suite would pass without waiting for
    # anything. Each scripting gets a gate of its own.
    stand_in.discovery_misbehaves(Misbehaviour.slow())
    stand_in.release()

    assert (await asking(stand_in, "/.well-known/openid-configuration")).status_code == 200

    stand_in.discovery_misbehaves(Misbehaviour.slow())
    try:
        with pytest.raises(httpx.TimeoutException):
            await asking(stand_in, "/.well-known/openid-configuration", timeout=0.25)
    finally:
        stand_in.release()


@asyncio_test
async def test_a_release_opens_what_was_already_waiting(stand_in: StandInProvider) -> None:
    stand_in.discovery_misbehaves(Misbehaviour.slow())
    stand_in.release()

    answer = await asking(stand_in, "/.well-known/openid-configuration")

    assert answer.status_code == 200


@asyncio_test
async def test_a_wrong_secret_does_not_burn_the_code(stand_in: StandInProvider) -> None:
    # A real authorization server authenticates the client before it redeems
    # anything. A stand-in that popped the code first would answer
    # invalid_client once and invalid_grant ever after, so a test of "the
    # secret was wrong" would be reading the wrong refusal on a retry.
    code = await code_for(stand_in)

    refused = await redeeming(stand_in, code, secret="not-the-secret")
    accepted = await redeeming(stand_in, code, secret=stand_in.client_secret)

    assert refused.status_code == 401
    assert refused.json()["error"] == "invalid_client"
    assert accepted.status_code == 200
    assert "id_token" in accepted.json()


@asyncio_test
async def test_a_code_is_still_single_use(stand_in: StandInProvider) -> None:
    code = await code_for(stand_in)

    first = await redeeming(stand_in, code, secret=stand_in.client_secret)
    second = await redeeming(stand_in, code, secret=stand_in.client_secret)

    assert first.status_code == 200
    assert second.status_code == 400
    assert second.json()["error"] == "invalid_grant"


@asyncio_test
async def test_a_gzip_bomb_is_small_on_the_wire_and_large_off_it(
    stand_in: StandInProvider,
) -> None:
    # The stand-in's side of the bound: if what it sent were not far bigger
    # inflated than compressed, the adapter's refusal would prove nothing.
    stand_in.discovery_misbehaves(Misbehaviour.gzip_bomb(1024 * 1024))

    answer = await asking(stand_in, "/.well-known/openid-configuration")

    assert answer.headers["content-encoding"] == "gzip"
    assert int(answer.headers["content-length"]) < 8 * 1024
    assert len(answer.content) == 1024 * 1024


@asyncio_test
async def test_a_chunked_answer_declares_no_length(stand_in: StandInProvider) -> None:
    stand_in.discovery_misbehaves(Misbehaviour.oversized(32 * 1024, chunked=True))

    answer = await asking(stand_in, "/.well-known/openid-configuration")

    assert "content-length" not in answer.headers
    assert len(json.loads(answer.content)["padding"]) == 32 * 1024


@asyncio_test
async def test_an_overstated_length_is_a_header_that_lies(stand_in: StandInProvider) -> None:
    stand_in.discovery_misbehaves(Misbehaviour.overstated(10 * 1024 * 1024))

    # The body never arrives, because there is not that much of it: a reader
    # that believes the header refuses at once, and one that does not waits.
    with pytest.raises(httpx.TimeoutException):
        await asking(stand_in, "/.well-known/openid-configuration", timeout=0.4)
