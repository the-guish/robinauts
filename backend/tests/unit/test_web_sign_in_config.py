# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

from __future__ import annotations

import tomllib

import pytest

from robinauts.controller.contract.domain import ConfigError
from robinauts.web.sign_in import AllowEntry, Matcher, ProviderConfig, parse_sign_in

# The example of docs/specs/sign-in.md, "Details likely to change", without its [[admin]]
# table: roles are not in this release.
EXAMPLE = """
public_url = "https://robinauts.example.com"
session_hours = 12

[providers.google]
title = "Google"
issuer = "https://accounts.google.com"
client_id = "..."
client_secret_env = "ROBINAUTS_GOOGLE_SECRET"

[providers.okta]
title = "Okta"
issuer = "https://example.okta.com/oauth2/default"
client_id = "..."
client_secret_env = "ROBINAUTS_OKTA_SECRET"
scopes = ["openid", "email", "profile", "groups"]
groups_claim = "groups"

[[allow]]
provider = "google"
hosted_domain = "example.com"

[[allow]]
provider = "okta"
group = "robinauts-users"
"""

THREE_MISTAKES = """
public_url = "http://robinauts.example.com"

[providers.google]
title = "Google"
issuer = "https://accounts.google.com"
client_id = "..."
client_secret_env = "ROBINAUTS_GOOGLE_SECRET"

[[allow]]
provider = "google"
everyone = true

[[admin]]
provider = "google"
email = "ada@example.com"
"""


def test_the_spec_example_parses_to_its_records() -> None:
    config = parse_sign_in(tomllib.loads(EXAMPLE))
    assert config is not None
    assert config.public_url == "https://robinauts.example.com"
    assert config.session_hours == 12
    assert config.providers == {
        "google": ProviderConfig(
            id="google",
            title="Google",
            issuer="https://accounts.google.com",
            client_id="...",
            client_secret_env="ROBINAUTS_GOOGLE_SECRET",
        ),
        "okta": ProviderConfig(
            id="okta",
            title="Okta",
            issuer="https://example.okta.com/oauth2/default",
            client_id="...",
            client_secret_env="ROBINAUTS_OKTA_SECRET",
            scopes=("openid", "email", "profile", "groups"),
            groups_claim="groups",
        ),
    }
    assert config.providers["google"].scopes == ("openid", "email", "profile")
    assert config.allow == (
        AllowEntry("google", Matcher.HOSTED_DOMAIN, "example.com"),
        AllowEntry("okta", Matcher.GROUP, "robinauts-users"),
    )


def test_names_every_problem_at_once() -> None:
    document = tomllib.loads(THREE_MISTAKES)
    with pytest.raises(ConfigError) as raised:
        parse_sign_in(document)
    message = str(raised.value)
    assert "public_url: 'http://robinauts.example.com' is not https" in message
    assert "allow 1: everyone is refused for Google" in message
    assert "admin: roles are not in this release" in message


@pytest.mark.parametrize(
    ("given", "days"),
    [({}, 30), ({"device_days": 0}, 0), ({"device_days": 7}, 7), ({"device_days": 0.5}, 0.5)],
)
def test_device_days_is_thirty_unless_named_and_zero_is_no_devices(
    given: dict[str, object], days: float
) -> None:
    config = parse_sign_in({**tomllib.loads(EXAMPLE), **given})
    assert config is not None
    assert config.device_days == days


@pytest.mark.parametrize("days", [-1, True, "30", float("nan"), float("inf"), [30]])
def test_device_days_that_is_not_a_number_of_days_is_refused(days: object) -> None:
    with pytest.raises(ConfigError, match="device_days: .* is not a number of days, 0 or more"):
        parse_sign_in({**tomllib.loads(EXAMPLE), "device_days": days})


def test_device_days_alone_is_a_sign_in_configuration() -> None:
    with pytest.raises(ConfigError, match="public_url"):
        parse_sign_in({"device_days": 30})
