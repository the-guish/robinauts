# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The exchange against the stand-in provider, which listens on a port the operating system
picks. Marked ``io``; nothing sleeps."""

from __future__ import annotations

import base64
import hashlib
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from aio import asyncio_test
from robinauts.web.oidc import DISCOVERY_PATH, Claims, Exchange, random_secret
from robinauts.web.sign_in import ProviderConfig, SignInConfig, SignInError, SignInErrorCode
from standin import Misbehaviour, StandInProvider, redirect_from

pytestmark = pytest.mark.io

PUBLIC_URL = "http://127.0.0.1:8000"
SECRET_ENV = "ROBINAUTS_STAND_IN_SECRET"
STATE, NONCE, VERIFIER = random_secret(), random_secret(), random_secret()


@pytest.fixture
def stand_in() -> Iterator[StandInProvider]:
    with StandInProvider() as provider:
        provider.person["groups"] = ["robinauts-users"]
        yield provider


@asynccontextmanager
async def exchange_with(stand_in: StandInProvider) -> AsyncIterator[Exchange]:
    okta = ProviderConfig(
        id="okta",
        title="Okta",
        issuer=stand_in.issuer,
        client_id=stand_in.client_id,
        client_secret_env=SECRET_ENV,
        scopes=("openid", "email", "profile", "groups"),
        groups_claim="groups",
    )
    config = SignInConfig(public_url=PUBLIC_URL, providers={"okta": okta}, allow=())
    exchange = Exchange(config, {SECRET_ENV: stand_in.client_secret}.get)
    try:
        yield exchange
    finally:
        await exchange.aclose()


async def signed_in(exchange: Exchange) -> Claims:
    """Begin, follow the authorization URL as a browser would, and complete with the code."""
    url = await exchange.begin("okta", state=STATE, nonce=NONCE, verifier=VERIFIER)
    code = parse_qs(urlsplit(await redirect_from(url)).query)["code"][0]
    return await exchange.complete(
        "okta", code=code, nonce=NONCE, verifier=VERIFIER, now=datetime.now(UTC)
    )


@asyncio_test
async def test_the_authorization_url_carries_state_nonce_the_challenge_and_the_redirect_uri(
    stand_in: StandInProvider,
) -> None:
    async with exchange_with(stand_in) as exchange:
        url = await exchange.begin("okta", state=STATE, nonce=NONCE, verifier=VERIFIER)

    endpoint, _, query = url.partition("?")
    digest = hashlib.sha256(VERIFIER.encode("ascii")).digest()
    assert endpoint == f"{stand_in.issuer}/authorize"
    assert {name: values[0] for name, values in parse_qs(query).items()} == {
        "response_type": "code",
        "client_id": stand_in.client_id,
        "redirect_uri": f"{PUBLIC_URL}/auth/callback/okta",
        "scope": "openid email profile groups",
        "state": STATE,
        "nonce": NONCE,
        "code_challenge": base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii"),
        "code_challenge_method": "S256",
    }


@asyncio_test
async def test_the_code_is_exchanged_for_the_claims_the_provider_wrote(
    stand_in: StandInProvider,
) -> None:
    async with exchange_with(stand_in) as exchange:
        claims = await signed_in(exchange)

    assert claims == Claims(
        subject="248289761001",
        name="Ada Lovelace",
        email="ada@example.com",
        hosted_domain=None,
        groups=("robinauts-users",),
    )
    assert stand_in.paths.count(DISCOVERY_PATH) == 1
    assert stand_in.exchanged[-1]["authorization"].startswith("Basic ")


@pytest.mark.parametrize(
    "tamper",
    [{"nonce": "another"}, {"exp": int(time.time()) - 10}, {"aud": "another-client"}],
    ids=["nonce", "exp", "aud"],
)
@asyncio_test
async def test_a_token_that_is_not_this_sign_ins_is_an_invalid_id_token(
    stand_in: StandInProvider, tamper: dict[str, Any]
) -> None:
    stand_in.tamper.update(tamper)
    async with exchange_with(stand_in) as exchange:
        with pytest.raises(SignInError) as refused:
            await signed_in(exchange)

    assert refused.value.code is SignInErrorCode.INVALID_ID_TOKEN


@asyncio_test
async def test_discovery_naming_another_issuer_is_unavailable_and_not_kept(
    stand_in: StandInProvider,
) -> None:
    stand_in.discovery["issuer"] = "https://elsewhere.example.com"
    async with exchange_with(stand_in) as exchange:
        with pytest.raises(SignInError) as refused:
            await exchange.begin("okta", state=STATE, nonce=NONCE, verifier=VERIFIER)
        stand_in.discovery.clear()
        await exchange.begin("okta", state=STATE, nonce=NONCE, verifier=VERIFIER)

    assert refused.value.code is SignInErrorCode.PROVIDER_UNAVAILABLE
    assert "https://elsewhere.example.com" in refused.value.detail


@pytest.mark.parametrize(
    ("how", "code"),
    [
        (Misbehaviour.server_error(), SignInErrorCode.PROVIDER_UNAVAILABLE),
        (Misbehaviour.not_json(), SignInErrorCode.PROVIDER_UNAVAILABLE),
        (Misbehaviour.oauth_error(), SignInErrorCode.PROVIDER_REFUSED),
    ],
    ids=["500", "not-json", "oauth-error"],
)
@asyncio_test
async def test_a_token_endpoint_that_does_not_answer_a_token_is_named_for_why(
    stand_in: StandInProvider, how: Misbehaviour, code: SignInErrorCode
) -> None:
    stand_in.token_misbehaves(how)
    async with exchange_with(stand_in) as exchange:
        with pytest.raises(SignInError) as refused:
            await signed_in(exchange)

    assert refused.value.code is code
