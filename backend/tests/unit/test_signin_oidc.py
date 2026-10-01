# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The authorization request: what it carries, and what its endpoint may not."""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import pytest

from robinauts.legacy.core import (
    AUTHORIZATION_PARAMETERS,
    MAX_CODE_CHARS,
    TOKEN_PARAMETERS,
    authorization_url,
    parameters_taken,
    pkce_challenge,
)
from robinauts.legacy.domain import ProviderConfig

GOOGLE = ProviderConfig(
    id="google",
    title="Google",
    issuer="https://accounts.google.com",
    client_id="cid.apps.googleusercontent.com",
    client_secret_env="ROBINAUTS_GOOGLE_SECRET",
)
OKTA = ProviderConfig(
    id="okta",
    title="Okta",
    issuer="https://example.okta.com/oauth2/default",
    client_id="okta-client",
    client_secret_env="ROBINAUTS_OKTA_SECRET",
    scopes=("openid", "email", "profile", "groups"),
    groups_claim="groups",
)
VERIFIER = "v" * 64
REDIRECT = "https://robinauts.example.com/auth/callback/google"


def built(endpoint: str, provider: ProviderConfig = GOOGLE, **changes: str) -> str:
    call = {"state": "the-state", "nonce": "the-nonce", "verifier": VERIFIER}
    call.update(changes)
    return authorization_url(endpoint, provider, redirect_uri=REDIRECT, **call)


def test_the_request_carries_what_the_specification_asks_for() -> None:
    url = built("https://accounts.google.com/authorize")

    assert url.startswith("https://accounts.google.com/authorize?")
    assert parse_qs(urlsplit(url).query) == {
        "response_type": ["code"],
        "client_id": [GOOGLE.client_id],
        "redirect_uri": [REDIRECT],
        "scope": ["openid email profile"],
        "state": ["the-state"],
        "nonce": ["the-nonce"],
        "code_challenge": [pkce_challenge(VERIFIER)],
        "code_challenge_method": ["S256"],
    }


def test_the_verifier_itself_is_never_sent() -> None:
    # Only its SHA-256 goes out; that is the whole of what PKCE is.
    url = built("https://accounts.google.com/authorize")

    assert VERIFIER not in url
    assert pkce_challenge(VERIFIER) in url


def test_a_provider_is_asked_for_the_scopes_it_was_configured_with() -> None:
    url = built("https://example.okta.com/oauth2/default/authorize", OKTA)

    assert parse_qs(urlsplit(url).query)["scope"] == ["openid email profile groups"]


def test_an_endpoint_with_a_query_of_its_own_keeps_it() -> None:
    url = built("https://accounts.google.com/authorize?flavour=corp")

    assert "/authorize?flavour=corp&response_type=code" in url
    assert parse_qs(urlsplit(url).query)["flavour"] == ["corp"]


def test_what_a_request_carries_is_escaped() -> None:
    url = built("https://accounts.google.com/authorize", state="a&b=c d")

    assert "state=a%26b%3Dc+d" in url
    assert parse_qs(urlsplit(url).query)["state"] == ["a&b=c d"]
    assert parse_qs(urlsplit(url).query)["response_type"] == ["code"]


@pytest.mark.parametrize("name", sorted(AUTHORIZATION_PARAMETERS))
def test_an_endpoint_that_already_sets_one_of_ours_is_named(name: str) -> None:
    endpoint = f"https://accounts.google.com/authorize?{name}=theirs"

    assert parameters_taken(endpoint, AUTHORIZATION_PARAMETERS) == (name,)


def test_the_comparison_ignores_case_as_a_server_may() -> None:
    endpoint = "https://accounts.google.com/authorize?Redirect_URI=https://evil.example"

    assert parameters_taken(endpoint, AUTHORIZATION_PARAMETERS) == ("Redirect_URI",)


def test_everything_taken_is_named_at_once_and_in_order() -> None:
    endpoint = "https://accounts.google.com/authorize?state=x&flavour=corp&client_id=evil"

    assert parameters_taken(endpoint, AUTHORIZATION_PARAMETERS) == ("client_id", "state")


def test_an_endpoint_that_takes_nothing_of_ours_is_left_alone() -> None:
    assert (
        parameters_taken("https://accounts.google.com/authorize?flavour=corp", TOKEN_PARAMETERS)
        == ()
    )
    assert parameters_taken("https://accounts.google.com/authorize", AUTHORIZATION_PARAMETERS) == ()


@pytest.mark.parametrize("name", sorted(TOKEN_PARAMETERS))
def test_a_token_endpoint_may_not_name_what_the_token_request_sends(name: str) -> None:
    endpoint = f"https://accounts.google.com/token?{name.upper()}=theirs"

    assert parameters_taken(endpoint, TOKEN_PARAMETERS) == (name.upper(),)


def test_the_two_sets_are_what_the_two_requests_send() -> None:
    assert "client_secret" in TOKEN_PARAMETERS
    assert "client_secret" not in AUTHORIZATION_PARAMETERS
    assert "nonce" in AUTHORIZATION_PARAMETERS
    assert MAX_CODE_CHARS > 1000
