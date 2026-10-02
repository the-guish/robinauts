# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The rules of the sign-in flow that need no provider: the allow list, where a sign-in lands,
and the local development mode's user, whom no session hands out."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from aio import asyncio_test
from robinauts.controller.composition import compose
from robinauts.controller.contract.domain import Config, StorageConfig, StorageKind, UserSession
from robinauts.web.app import LOCAL_IDENTITY
from robinauts.web.oidc import Claims
from robinauts.web.sign_in import (
    AllowEntry,
    Matcher,
    SignIn,
    SignInConfig,
    is_allowed,
    random_secret,
    safe_return_to,
    secret_hash,
)

ADA = Claims(
    subject="248289761001",
    name="Ada Lovelace",
    email="Ada@Example.com",
    hosted_domain="example.com",
    groups=("robinauts-users",),
)


@pytest.mark.parametrize(
    ("matcher", "value"),
    [
        (Matcher.EVERYONE, None),
        (Matcher.SUBJECT, "248289761001"),
        (Matcher.EMAIL, "ada@EXAMPLE.com"),
        (Matcher.EMAIL_DOMAIN, "EXAMPLE.com"),
        (Matcher.HOSTED_DOMAIN, "example.com"),
        (Matcher.GROUP, "robinauts-users"),
    ],
)
def test_each_matcher_lets_in_the_person_it_names(matcher: Matcher, value: str | None) -> None:
    assert is_allowed([AllowEntry("okta", matcher, value)], "okta", ADA)


@pytest.mark.parametrize(
    ("matcher", "value"),
    [
        (Matcher.SUBJECT, "1"),
        (Matcher.EMAIL, "eve@example.com"),
        (Matcher.EMAIL_DOMAIN, "example.org"),
        (Matcher.HOSTED_DOMAIN, "example.org"),
        (Matcher.GROUP, "robinauts-admins"),
    ],
)
def test_and_nobody_else(matcher: Matcher, value: str) -> None:
    assert not is_allowed([AllowEntry("okta", matcher, value)], "okta", ADA)


def test_an_address_the_provider_did_not_verify_matches_no_email_entry() -> None:
    allow = [
        AllowEntry("okta", Matcher.EMAIL, "ada@example.com"),
        AllowEntry("okta", Matcher.EMAIL_DOMAIN, "example.com"),
    ]

    assert not is_allowed(allow, "okta", replace(ADA, email=None))


def test_an_entry_of_another_provider_lets_nobody_in() -> None:
    assert not is_allowed([AllowEntry("google", Matcher.SUBJECT, ADA.subject)], "okta", ADA)


def test_one_matching_entry_is_enough() -> None:
    allow = [
        AllowEntry("okta", Matcher.GROUP, "robinauts-admins"),
        AllowEntry("okta", Matcher.SUBJECT, ADA.subject),
    ]

    assert is_allowed(allow, "okta", ADA)


@pytest.mark.parametrize(
    ("given", "kept"),
    [
        ("/#/chats/7", "/#/chats/7"),
        ("/", "/"),
        (None, "/"),
        ("", "/"),
        ("//evil.example.com/", "/"),
        ("https://evil.example.com/", "/"),
        ("chats", "/"),
    ],
)
def test_a_sign_in_returns_only_to_a_path_of_this_origin(given: str | None, kept: str) -> None:
    assert safe_return_to(given) == kept


@asyncio_test
async def test_a_session_naming_the_local_user_signs_nobody_in() -> None:
    composed = compose(Config(), storage=StorageConfig(StorageKind.IN_MEMORY), secret_for={}.get)
    local = await composed.controller.ensure_user(LOCAL_IDENTITY)
    now = datetime.now(UTC)
    secret = random_secret()
    session = UserSession(
        uuid.uuid4(), local.id, secret_hash(secret), now, now + timedelta(hours=1)
    )
    await composed.credentials.add_user_session(session)
    config = SignInConfig(public_url="http://127.0.0.1:8000", providers={}, allow=())
    flow = SignIn(
        config, credentials=composed.credentials, exchange=None, controller=composed.controller
    )

    assert await composed.credentials.resolve_user_session(secret_hash(secret), now) == local
    assert await flow.resolve(secret, now) is None
