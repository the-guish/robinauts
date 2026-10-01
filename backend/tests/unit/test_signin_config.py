# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The sign-in configuration: what is refused, and that it is all refused at once."""

from typing import Any

import pytest

from robinauts.legacy.core import parse_sign_in_config
from robinauts.legacy.domain import (
    DEFAULT_SCOPES,
    DEFAULT_SESSION_HOURS,
    MAX_SESSION_HOURS,
    AllowEntry,
    ConfigError,
    Matcher,
)

GOOGLE_TABLE = {
    "title": "Google",
    "issuer": "https://accounts.google.com",
    "client_id": "cid.apps.googleusercontent.com",
    "client_secret_env": "ROBINAUTS_GOOGLE_SECRET",
}
OKTA_TABLE = {
    "title": "Okta",
    "issuer": "https://example.okta.com/oauth2/default",
    "client_id": "cid",
    "client_secret_env": "ROBINAUTS_OKTA_SECRET",
    "scopes": ["openid", "email", "profile", "groups"],
    "groups_claim": "groups",
}


def data(**changes: Any) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "public_url": "https://robinauts.example.com",
        "providers": {"okta": dict(OKTA_TABLE)},
        "allow": [{"provider": "okta", "group": "robinauts-users"}],
    }
    raw.update(changes)
    return {key: value for key, value in raw.items() if value is not ...}


def problems(**changes: Any) -> list[str]:
    with pytest.raises(ConfigError) as raised:
        parse_sign_in_config(data(**changes))
    return list(raised.value.problems)


def one_problem(**changes: Any) -> str:
    found = problems(**changes)
    assert len(found) == 1, found
    return found[0]


# --- what a good configuration becomes -------------------------------------


def test_the_example_of_the_spec_is_read_into_domain_records() -> None:
    config = parse_sign_in_config(
        {
            "public_url": "https://robinauts.example.com",
            "session_hours": 8,
            "providers": {"google": dict(GOOGLE_TABLE), "okta": dict(OKTA_TABLE)},
            "allow": [
                {"provider": "google", "hosted_domain": "example.com"},
                {"provider": "okta", "group": "robinauts-users"},
            ],
        }
    )
    assert config.public_url == "https://robinauts.example.com"
    assert config.session_hours == 8
    assert set(config.providers) == {"google", "okta"}
    assert config.providers["google"].is_google
    assert config.providers["okta"].groups_claim == "groups"
    assert config.providers["okta"].scopes == ("openid", "email", "profile", "groups")
    assert config.allow == (
        AllowEntry("google", Matcher.HOSTED_DOMAIN, "example.com"),
        AllowEntry("okta", Matcher.GROUP, "robinauts-users"),
    )


def test_what_is_left_out_takes_its_default() -> None:
    config = parse_sign_in_config(
        data(
            providers={"google": dict(GOOGLE_TABLE)},
            allow=[{"provider": "google", "hosted_domain": "example.com"}],
        )
    )
    google = config.providers["google"]
    assert config.session_hours == DEFAULT_SESSION_HOURS
    assert google.scopes == DEFAULT_SCOPES
    assert google.groups_claim is None
    assert google.token_endpoint_auth == "client_secret_basic"


def test_a_provider_without_a_title_is_called_by_its_id() -> None:
    table = {key: value for key, value in OKTA_TABLE.items() if key != "title"}
    config = parse_sign_in_config(data(providers={"okta": table}))
    assert config.providers["okta"].title == "okta"


def test_the_secret_is_named_and_never_read_here() -> None:
    config = parse_sign_in_config(data())
    okta = config.providers["okta"]
    assert okta.client_secret_env == "ROBINAUTS_OKTA_SECRET"
    assert not hasattr(okta, "client_secret")
    assert "ROBINAUTS_OKTA_SECRET" in repr(config)


def test_urls_come_out_in_one_form() -> None:
    config = parse_sign_in_config(
        data(
            public_url="HTTPS://Robinauts.Example.com:443/",
            providers={
                "okta": {**OKTA_TABLE, "issuer": "https://Example.okta.com/oauth2/default/"}
            },
        )
    )
    assert config.public_url == "https://robinauts.example.com"
    assert config.providers["okta"].issuer == "https://example.okta.com/oauth2/default"


# --- everything at once ----------------------------------------------------


def test_every_problem_is_reported_at_once() -> None:
    found = problems(
        public_url="https://robinauts.example.com/app",
        session_hours=0,
        providers={"okta": {**OKTA_TABLE, "client_id": "", "typo": 1}},
        allow=[{"provider": "nobody", "group": "g"}],
        unknown_top_level=True,
    )
    assert len(found) == 6, found
    assert any("unknown_top_level" in problem for problem in found)
    assert any(problem.startswith("public_url:") for problem in found)
    assert any(problem.startswith("session_hours:") for problem in found)
    assert any("providers.okta" in problem and "typo" in problem for problem in found)
    assert any(problem.startswith("allow 1:") for problem in found)


def test_a_config_error_names_where_each_problem_is() -> None:
    with pytest.raises(ConfigError) as raised:
        parse_sign_in_config({})
    assert len(raised.value.problems) == 2
    assert "invalid configuration" in str(raised.value)


# --- the top level ---------------------------------------------------------


def test_an_unknown_key_is_a_mistake_not_something_to_ignore() -> None:
    assert "unknown key 'sesion_hours'" in one_problem(sesion_hours=12)


def test_admin_entries_are_refused_while_roles_are_not_here() -> None:
    problem = one_problem(admin=[{"provider": "okta", "group": "robinauts-admins"}])
    assert problem.startswith("admin:")
    assert "unknown key" not in problem


@pytest.mark.parametrize(
    "public_url",
    [
        ...,
        "",
        42,
        "robinauts.example.com",
        "http://robinauts.example.com",
        "https://robinauts.example.com/app",
        "https://",
        "ftp://robinauts.example.com",
        "https://user:pw@robinauts.example.com",
        "https://robinauts.example.com/?q=1",
    ],
)
def test_the_public_url_is_an_origin_over_https(public_url: Any) -> None:
    assert one_problem(public_url=public_url).startswith("public_url:")


def test_http_is_allowed_on_loopback_alone() -> None:
    config = parse_sign_in_config(data(public_url="http://localhost:8000"))
    assert config.public_url == "http://localhost:8000"
    assert not config.secure


@pytest.mark.parametrize("hours", [0, -1, "12", True, MAX_SESSION_HOURS + 1, float("inf")])
def test_session_hours_is_a_number_of_hours_within_bounds(hours: Any) -> None:
    assert one_problem(session_hours=hours).startswith("session_hours:")


def test_session_hours_may_be_a_fraction_of_one() -> None:
    assert parse_sign_in_config(data(session_hours=0.5)).session_hours == 0.5


# --- providers -------------------------------------------------------------


def test_a_deployment_with_no_provider_is_a_deployment_nobody_can_reach() -> None:
    assert one_problem(providers=..., allow=[]).startswith("providers:")
    assert one_problem(providers={}, allow=[]).startswith("providers:")


def test_providers_is_a_table_of_providers() -> None:
    assert one_problem(providers=["google"], allow=[]).startswith("providers:")


@pytest.mark.parametrize("provider_id", ["", "Google", "a" * 41, "-google", "gôogle", "go gle"])
def test_a_provider_id_is_short_and_lower_case(provider_id: str) -> None:
    found = problems(providers={provider_id: dict(OKTA_TABLE)}, allow=[])
    assert any("an id is" in problem for problem in found)


@pytest.mark.parametrize("key", ["issuer", "client_id", "client_secret_env"])
def test_a_provider_needs_its_issuer_client_and_secret_name(key: str) -> None:
    table = {name: value for name, value in OKTA_TABLE.items() if name != key}
    assert any(
        problem.startswith(f"providers.okta.{key}:")
        for problem in problems(providers={"okta": table})
    )


def test_a_provider_that_is_not_a_table_is_refused() -> None:
    assert one_problem(providers={"okta": "https://example.okta.com"}) == "providers.okta: a table"


def test_an_issuer_must_be_a_url_that_can_be_compared() -> None:
    assert any(
        problem.startswith("providers.okta.issuer:")
        for problem in problems(providers={"okta": {**OKTA_TABLE, "issuer": "example.okta.com"}})
    )


def test_the_secret_name_is_a_variable_name_not_a_secret() -> None:
    table = {**OKTA_TABLE, "client_secret_env": "s3cret-value!"}
    problem = one_problem(providers={"okta": table})
    assert problem.startswith("providers.okta.client_secret_env:")
    assert "s3cret-value!" not in problem


@pytest.mark.parametrize(
    "scopes", [[], "openid", ["email", "profile"], ["openid", ""], ["openid", 1]]
)
def test_scopes_are_names_and_must_include_openid(scopes: Any) -> None:
    assert any(
        problem.startswith("providers.okta.scopes:")
        for problem in problems(providers={"okta": {**OKTA_TABLE, "scopes": scopes}})
    )


def test_the_token_endpoint_auth_is_one_of_the_two_ways() -> None:
    table = {**OKTA_TABLE, "token_endpoint_auth": "private_key_jwt"}
    assert one_problem(providers={"okta": table}).startswith("providers.okta.token_endpoint_auth:")
    config = parse_sign_in_config(
        data(providers={"okta": {**OKTA_TABLE, "token_endpoint_auth": "client_secret_post"}})
    )
    assert config.providers["okta"].token_endpoint_auth == "client_secret_post"


# --- the allow list --------------------------------------------------------


def test_providers_without_an_allow_entry_let_nobody_in() -> None:
    assert one_problem(allow=[]) == "allow: no entry, so nobody could sign in"
    assert one_problem(allow=...) == "allow: no entry, so nobody could sign in"


def test_entries_that_are_there_and_wrong_say_so_without_also_saying_none_is_there() -> None:
    found = problems(allow=[{"provider": "okta", "group": ""}])
    assert not any("no entry" in problem for problem in found)


def test_the_allow_list_is_an_array_of_tables() -> None:
    assert "allow: an array" in one_problem(allow={"provider": "okta"})
    assert one_problem(allow=["okta"]) == "allow 1: a table"


def test_an_entry_names_a_provider_that_is_configured() -> None:
    assert "not one of [providers]" in one_problem(allow=[{"provider": "google", "subject": "1"}])
    assert "not one of [providers]" in one_problem(allow=[{"everyone": True}])


def test_an_entry_carries_exactly_one_matcher() -> None:
    assert "exactly one of" in one_problem(allow=[{"provider": "okta"}])
    assert "exactly one of" in one_problem(
        allow=[{"provider": "okta", "group": "g", "subject": "s"}]
    )


def test_an_unknown_key_in_an_entry_is_a_mistake() -> None:
    found = problems(allow=[{"provider": "okta", "grupo": "g"}])
    assert any("unknown key 'grupo'" in problem for problem in found)


def test_everyone_is_written_as_a_flag() -> None:
    assert parse_sign_in_config(data(allow=[{"provider": "okta", "everyone": True}])).allow == (
        AllowEntry("okta", Matcher.EVERYONE),
    )
    assert "everyone = true" in one_problem(allow=[{"provider": "okta", "everyone": "yes"}])
    assert "everyone = true" in one_problem(allow=[{"provider": "okta", "everyone": False}])


def test_everyone_is_refused_for_google() -> None:
    # At Okta everyone is the company's tenant; at Google it is the world.
    problem = one_problem(
        providers={"google": dict(GOOGLE_TABLE)},
        allow=[{"provider": "google", "everyone": True}],
    )
    assert "everyone is refused for Google" in problem
    assert "hosted_domain" in problem


@pytest.mark.parametrize("value", ["", 1, True, ["a"]])
def test_every_other_matcher_takes_a_non_empty_string(value: Any) -> None:
    assert "non-empty string" in one_problem(allow=[{"provider": "okta", "group": value}])


def test_email_domain_is_refused_for_google() -> None:
    problem = one_problem(
        providers={"google": dict(GOOGLE_TABLE)},
        allow=[{"provider": "google", "email_domain": "example.com"}],
    )
    assert "email_domain is refused for Google" in problem


@pytest.mark.parametrize(
    "issuer",
    [
        "https://accounts.google.com",
        "https://accounts.google.com/",
        "https://accounts.google.com.",
        "HTTPS://Accounts.Google.COM.:443/",
    ],
)
def test_google_is_google_however_its_issuer_is_spelt(issuer: str) -> None:
    # Each spelling refuses email_domain and everyone and accepts
    # hosted_domain: the rules that say the configuration knows this is
    # Google. (The bare host, which Google also uses for ``iss``, is not a
    # configurable issuer: a configured one is an https URL.)
    table = {**GOOGLE_TABLE, "issuer": issuer}
    assert "email_domain is refused for Google" in one_problem(
        providers={"google": table},
        allow=[{"provider": "google", "email_domain": "example.com"}],
    )
    assert "everyone is refused for Google" in one_problem(
        providers={"google": table},
        allow=[{"provider": "google", "everyone": True}],
    )
    config = parse_sign_in_config(
        data(
            providers={"google": table},
            allow=[{"provider": "google", "hosted_domain": "example.com"}],
        )
    )
    assert config.providers["google"].is_google
    assert config.providers["google"].issuer == "https://accounts.google.com"


def test_hosted_domain_is_refused_for_anyone_but_google() -> None:
    problem = one_problem(allow=[{"provider": "okta", "hosted_domain": "example.com"}])
    assert "hosted_domain is Google's hd claim" in problem


def test_a_group_entry_needs_the_claim_the_groups_come_in() -> None:
    # Without a groups_claim the provider sends no groups, so the entry would
    # match nobody, quietly, for as long as the deployment lived.
    table = {name: value for name, value in OKTA_TABLE.items() if name != "groups_claim"}
    problem = one_problem(providers={"okta": table})
    assert problem.startswith("allow 1:")
    assert "groups_claim" in problem


def test_a_group_entry_is_accepted_once_the_claim_is_named() -> None:
    config = parse_sign_in_config(data())
    assert config.allow == (AllowEntry("okta", Matcher.GROUP, "robinauts-users"),)


def test_a_group_entry_for_google_is_refused_since_google_sends_no_groups() -> None:
    problem = one_problem(
        providers={"google": dict(GOOGLE_TABLE)},
        allow=[{"provider": "google", "group": "robinauts-users"}],
    )
    assert "groups_claim" in problem


def test_googles_own_matchers_are_accepted() -> None:
    config = parse_sign_in_config(
        data(
            providers={"google": dict(GOOGLE_TABLE)},
            allow=[
                {"provider": "google", "hosted_domain": "example.com"},
                {"provider": "google", "email": "ada@gmail.com"},
                {"provider": "google", "subject": "1234"},
            ],
        )
    )
    assert len(config.allow) == 3


def test_a_provider_with_problems_of_its_own_still_has_its_entries_judged() -> None:
    found = problems(
        providers={"google": {**GOOGLE_TABLE, "client_id": ""}},
        allow=[{"provider": "google", "email_domain": "example.com"}],
    )
    assert any(problem.startswith("providers.google.client_id:") for problem in found)
    assert any("email_domain is refused for Google" in problem for problem in found)
    found = problems(
        providers={"google": {**GOOGLE_TABLE, "client_id": ""}},
        allow=[{"provider": "google", "everyone": True}],
    )
    assert any(problem.startswith("providers.google.client_id:") for problem in found)
    assert any("everyone is refused for Google" in problem for problem in found)


# --- an entry that could never match is a rule the operator believes in ----


@pytest.mark.parametrize(
    "address", ["ada", "ada@", "@example.com", "a@b@example.com", "ada example.com", "@"]
)
def test_an_email_entry_is_one_address(address: str) -> None:
    problem = one_problem(allow=[{"provider": "okta", "email": address}])
    assert problem.startswith("allow 1: email ")


@pytest.mark.parametrize(
    "domain",
    [
        "https://example.com",
        "example.com/staff",
        "ada@example.com",
        "example.com:443",
        "exa mple.com",
    ],
)
def test_a_domain_entry_is_a_bare_domain(domain: str) -> None:
    assert "allow 1: email_domain " in one_problem(
        allow=[{"provider": "okta", "email_domain": domain}]
    )


def test_a_hosted_domain_entry_is_a_bare_domain_too() -> None:
    problem = one_problem(
        providers={"google": dict(GOOGLE_TABLE)},
        allow=[{"provider": "google", "hosted_domain": "https://example.com"}],
    )
    assert problem.startswith("allow 1: hosted_domain ")


@pytest.mark.parametrize("matcher", ["email", "email_domain"])
def test_a_value_outside_ascii_is_refused_with_the_a_label_asked_for(matcher: str) -> None:
    value = "bücher.example" if matcher == "email_domain" else "ada@bücher.example"
    problem = one_problem(allow=[{"provider": "okta", matcher: value}])
    assert "A-label" in problem


def test_a_hosted_domain_outside_ascii_is_refused_too() -> None:
    problem = one_problem(
        providers={"google": dict(GOOGLE_TABLE)},
        allow=[{"provider": "google", "hosted_domain": "bücher.example"}],
    )
    assert "A-label" in problem


def test_the_a_label_form_is_accepted() -> None:
    config = parse_sign_in_config(
        data(allow=[{"provider": "okta", "email_domain": "xn--bcher-kva.example"}])
    )
    assert config.allow[0].value == "xn--bcher-kva.example"


@pytest.mark.parametrize("matcher", ["email", "email_domain"])
def test_an_address_entry_needs_the_scope_addresses_arrive_in(matcher: str) -> None:
    value = "example.com" if matcher == "email_domain" else "ada@example.com"
    table = {**OKTA_TABLE, "scopes": ["openid", "profile", "groups"]}
    problem = one_problem(providers={"okta": table}, allow=[{"provider": "okta", matcher: value}])
    assert "'email' among providers.okta.scopes" in problem


def test_the_default_scopes_ask_for_an_address() -> None:
    table = {name: value for name, value in OKTA_TABLE.items() if name != "scopes"}
    config = parse_sign_in_config(
        data(providers={"okta": table}, allow=[{"provider": "okta", "email": "ada@example.com"}])
    )
    assert config.providers["okta"].scopes == DEFAULT_SCOPES
    assert config.allow[0].value == "ada@example.com"


def test_entries_that_can_never_match_are_all_reported_at_once() -> None:
    found = problems(
        allow=[
            {"provider": "okta", "email": "ada"},
            {"provider": "okta", "email_domain": "https://example.com"},
            {"provider": "okta", "hosted_domain": "example.com"},
            {"provider": "okta", "group": "robinauts-users"},
        ]
    )
    assert len(found) == 3, found
    assert found[0].startswith("allow 1:")
    assert found[1].startswith("allow 2:")
    assert found[2].startswith("allow 3:")
