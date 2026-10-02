# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The ID token's claims, checked and read with no provider: decoded claims, a ``now`` and
the provider they are compared with."""

from __future__ import annotations

import base64
from datetime import UTC, datetime
from typing import Any

import pytest

from robinauts.web.oidc import Claims, checked_claims, read_id_token
from robinauts.web.sign_in import GOOGLE_ISSUER, ProviderConfig, SignInError, SignInErrorCode

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
NONCE = "the-nonce"

OKTA = ProviderConfig(
    id="okta",
    title="Okta",
    issuer="https://example.okta.com/oauth2/default",
    client_id="robinauts",
    client_secret_env="ROBINAUTS_OKTA_SECRET",
    groups_claim="groups",
)
GOOGLE = ProviderConfig(
    id="google",
    title="Google",
    issuer=GOOGLE_ISSUER,
    client_id="robinauts",
    client_secret_env="ROBINAUTS_GOOGLE_SECRET",
)


def token_claims(provider: ProviderConfig, **changes: Any) -> dict[str, Any]:
    """A token's claims that pass, with ``changes``; a change to ``None`` removes the claim."""
    claims = {
        "iss": provider.issuer,
        "aud": provider.client_id,
        "iat": NOW.timestamp(),
        "exp": NOW.timestamp() + 300,
        "nonce": NONCE,
        "sub": "subject-1",
        **changes,
    }
    return {name: value for name, value in claims.items() if value is not None}


def test_the_claims_of_an_okta_token_are_read() -> None:
    claims = token_claims(
        OKTA,
        name="Ada Lovelace",
        email="ada@example.com",
        email_verified=True,
        hd="example.com",
        groups=["robinauts-users", 7],
    )

    assert checked_claims(claims, OKTA, nonce=NONCE, now=NOW) == Claims(
        subject="subject-1",
        name="Ada Lovelace",
        email="ada@example.com",
        hosted_domain=None,
        groups=("robinauts-users",),
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"iat": NOW.timestamp() + 60},
        {"aud": ["another-client", "robinauts"], "azp": "robinauts"},
    ],
    ids=["iat-a-minute-ahead", "aud-a-list"],
)
def test_a_token_within_the_rules_is_accepted(changes: dict[str, Any]) -> None:
    claims = checked_claims(token_claims(OKTA, **changes), OKTA, nonce=NONCE, now=NOW)

    assert claims.subject == "subject-1"


@pytest.mark.parametrize(
    "changes",
    [
        {"iat": NOW.timestamp() + 61},
        {"sub": None},
        {"sub": ""},
        {"aud": ["another-client"]},
        {"azp": "another-client"},
        {"iss": "https://elsewhere.example.com"},
        {"exp": "tomorrow"},
        {"exp": float("nan")},
        {"iat": True},
        {"aud": {"robinauts": True}},
        {"nonce": ["the-nonce"]},
        {"sub": 7},
    ],
    ids=[
        "iat-past-the-skew",
        "no-sub",
        "empty-sub",
        "aud-a-list-without-us",
        "azp-another",
        "iss-another",
        "exp-text",
        "exp-nan",
        "iat-a-bool",
        "aud-an-object",
        "nonce-a-list",
        "sub-a-number",
    ],
)
def test_a_token_outside_the_rules_is_an_invalid_id_token(changes: dict[str, Any]) -> None:
    with pytest.raises(SignInError) as refused:
        checked_claims(token_claims(OKTA, **changes), OKTA, nonce=NONCE, now=NOW)

    assert refused.value.code is SignInErrorCode.INVALID_ID_TOKEN


@pytest.mark.parametrize(
    ("email", "hd", "kept"),
    [
        ("ada@example.com", None, False),
        ("ada@example.com", "example.com", True),
        ("ada@gmail.com", None, True),
    ],
    ids=["personal-account", "workspace", "gmail"],
)
def test_google_verifies_an_email_only_in_a_workspace_or_at_gmail(
    email: str, hd: str | None, kept: bool
) -> None:
    claims = token_claims(GOOGLE, email=email, email_verified=True, hd=hd)

    read = checked_claims(claims, GOOGLE, nonce=NONCE, now=NOW)

    assert read.email == (email if kept else None)


def test_an_email_not_verified_is_not_read() -> None:
    claims = token_claims(OKTA, email="ada@example.com", email_verified="true")

    assert checked_claims(claims, OKTA, nonce=NONCE, now=NOW).email is None


def unsigned(payload: bytes) -> str:
    return f"e30.{base64.urlsafe_b64encode(payload).rstrip(b'=').decode('ascii')}."


@pytest.mark.parametrize(
    "token",
    [None, "not-a-token", unsigned(b"not json"), unsigned(b'["a list"]'), "e30.!!!."],
    ids=["none", "one-part", "not-json", "not-an-object", "not-base64"],
)
def test_a_token_that_cannot_be_read_is_an_invalid_id_token(token: object) -> None:
    with pytest.raises(SignInError) as refused:
        read_id_token(token)

    assert refused.value.code is SignInErrorCode.INVALID_ID_TOKEN


def test_a_token_is_read_without_its_signature() -> None:
    assert read_id_token(unsigned(b'{"sub":"subject-1"}')) == {"sub": "subject-1"}
