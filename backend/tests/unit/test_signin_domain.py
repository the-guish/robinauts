# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The sign-in vocabulary: the error hierarchy and the records."""

import dataclasses
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest

from robinauts.legacy.domain import (
    DEFAULT_SESSION_HOURS,
    GOOGLE_ISSUER,
    MAX_SESSION_HOURS,
    AllowEntry,
    ConfigError,
    Identity,
    InvalidIdTokenError,
    InvalidValueError,
    Matcher,
    NotAllowedError,
    PendingLogin,
    ProviderConfig,
    RobinautsError,
    Session,
    SignInConfig,
    SignInError,
    SignInErrorCode,
    UnknownProviderError,
    User,
    is_google_issuer,
)

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)


def session(**changes: object) -> Session:
    fields: dict[str, object] = {
        "id": uuid.uuid4(),
        "user_id": uuid.uuid4(),
        "created_at": NOW,
        "expires_at": NOW + timedelta(hours=12),
    }
    fields.update(changes)
    return Session(**fields)  # type: ignore[arg-type]


def pending(**changes: object) -> PendingLogin:
    fields: dict[str, object] = {
        "provider": "okta",
        "nonce": "the-nonce",
        "verifier": "the-verifier",
        "created_at": NOW,
        "expires_at": NOW + timedelta(minutes=10),
    }
    fields.update(changes)
    return PendingLogin(**fields)  # type: ignore[arg-type]


def provider(**changes: object) -> ProviderConfig:
    fields: dict[str, object] = {
        "id": "okta",
        "title": "Okta",
        "issuer": "https://example.okta.com/oauth2/default",
        "client_id": "cid",
        "client_secret_env": "ROBINAUTS_OKTA_SECRET",
    }
    fields.update(changes)
    return ProviderConfig(**fields)  # type: ignore[arg-type]


def config(**changes: object) -> SignInConfig:
    fields: dict[str, object] = {
        "public_url": "https://robinauts.example.com",
        "providers": {"okta": provider()},
        "allow": (AllowEntry("okta", Matcher.EVERYONE),),
    }
    fields.update(changes)
    return SignInConfig(**fields)  # type: ignore[arg-type]


# --- the error hierarchy ---------------------------------------------------


def test_every_sign_in_error_is_a_robinauts_error() -> None:
    for error in (SignInError(SignInErrorCode.BUSY, "why"), ConfigError(["why"])):
        assert isinstance(error, RobinautsError)
    assert issubclass(InvalidValueError, RobinautsError)


def test_the_error_codes_are_the_ones_the_spec_fixes() -> None:
    assert {code.value for code in SignInErrorCode} == {
        "expired",
        "state_mismatch",
        "not_allowed",
        "unknown_provider",
        "busy",
        "provider_unavailable",
        "provider_refused",
        "invalid_id_token",
    }


def test_a_sign_in_error_keeps_the_code_apart_from_the_detail() -> None:
    error = SignInError(SignInErrorCode.PROVIDER_REFUSED, "okta said invalid_grant")
    assert error.code is SignInErrorCode.PROVIDER_REFUSED
    assert error.detail == "okta said invalid_grant"
    assert str(error) == "provider_refused: okta said invalid_grant"


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (NotAllowedError("nobody knows them"), SignInErrorCode.NOT_ALLOWED),
        (UnknownProviderError("no such provider"), SignInErrorCode.UNKNOWN_PROVIDER),
        (InvalidIdTokenError("iss"), SignInErrorCode.INVALID_ID_TOKEN),
    ],
)
def test_the_named_sign_in_errors_carry_their_code(
    error: SignInError, code: SignInErrorCode
) -> None:
    assert isinstance(error, SignInError)
    assert error.code is code


def test_a_config_error_holds_every_problem_and_shows_them_all() -> None:
    error = ConfigError(["public_url: missing", "allow: no entry"])
    assert error.problems == ("public_url: missing", "allow: no entry")
    assert "public_url: missing" in str(error)
    assert "allow: no entry" in str(error)


def test_a_config_error_without_a_problem_is_itself_a_mistake() -> None:
    with pytest.raises(InvalidValueError):
        ConfigError([])


# --- the records -----------------------------------------------------------


def test_an_identity_names_a_provider_and_a_subject() -> None:
    with pytest.raises(InvalidValueError):
        Identity(provider="", subject="s")
    with pytest.raises(InvalidValueError):
        Identity(provider="okta", subject="")


def test_an_allow_entry_of_everyone_compares_with_nothing() -> None:
    assert AllowEntry("okta", Matcher.EVERYONE).value is None
    with pytest.raises(InvalidValueError):
        AllowEntry("okta", Matcher.EVERYONE, "example.com")


@pytest.mark.parametrize(
    "matcher",
    [Matcher.SUBJECT, Matcher.EMAIL, Matcher.EMAIL_DOMAIN, Matcher.HOSTED_DOMAIN, Matcher.GROUP],
)
def test_every_other_allow_entry_needs_a_value(matcher: Matcher) -> None:
    with pytest.raises(InvalidValueError):
        AllowEntry("okta", matcher)
    with pytest.raises(InvalidValueError):
        AllowEntry("okta", matcher, "")


@pytest.mark.parametrize("matcher", ["everyone", "email", 0, None, True])
def test_an_allow_entry_matches_by_a_matcher_not_by_a_word(matcher: object) -> None:
    # Matcher is a StrEnum, so "everyone" == Matcher.EVERYONE while
    # "everyone" is not Matcher.EVERYONE: an entry built from the word would
    # carry a value nothing compared and let everyone in.
    with pytest.raises(InvalidValueError):
        AllowEntry("okta", matcher, "example.com")  # type: ignore[arg-type]


def test_an_identity_is_verified_by_a_flag_and_nothing_else() -> None:
    for truthy in ("no", "true", 1, [1]):
        with pytest.raises(InvalidValueError):
            Identity("okta", "00u1", email_verified=truthy)  # type: ignore[arg-type]
    assert Identity("okta", "00u1", email_verified=True).email_verified


def test_an_identitys_groups_are_names_and_a_word_is_not_its_letters() -> None:
    assert Identity("okta", "00u1", groups=["a", "b"]).groups == frozenset({"a", "b"})
    for bad in ("admins", 42, ["a", 1], ["a", ""], [None]):
        with pytest.raises(InvalidValueError):
            Identity("okta", "00u1", groups=bad)  # type: ignore[arg-type]


def test_a_pending_sign_in_prints_neither_its_nonce_nor_its_verifier() -> None:
    shown = repr(pending(nonce="NONCE-SECRET", verifier="VERIFIER-SECRET"))
    assert "NONCE-SECRET" not in shown
    assert "VERIFIER-SECRET" not in shown
    assert "okta" in shown


def test_no_other_record_prints_a_secret() -> None:
    # The records that exist hold no secret at all: a session is found by the
    # hash of the cookie's value, which is not in the record, and a provider
    # holds the name of a variable. This test is the place to notice if that
    # ever stops being true.
    assert "nonce" not in repr(session())
    for shown in (repr(session()), repr(User(id=uuid.uuid4(), provider="okta", subject="00u1"))):
        assert "secret" not in shown.lower()


def test_an_allow_entry_names_its_provider() -> None:
    with pytest.raises(InvalidValueError):
        AllowEntry("", Matcher.EVERYONE)


def test_a_provider_config_holds_the_name_of_the_secret_and_never_the_secret() -> None:
    names = {field.name for field in dataclasses.fields(ProviderConfig)}
    assert "client_secret_env" in names
    assert "client_secret" not in names
    # There is nowhere to put a secret: the record refuses the field itself.
    with pytest.raises(TypeError):
        provider(client_secret="s3cret")
    assert "ROBINAUTS_OKTA_SECRET" in repr(provider())


@pytest.mark.parametrize("name", ["id", "title", "issuer", "client_id", "client_secret_env"])
def test_a_provider_needs_every_one_of_its_names(name: str) -> None:
    with pytest.raises(InvalidValueError):
        provider(**{name: ""})


def test_an_empty_issuer_and_client_id_would_match_an_empty_token() -> None:
    # The point of the rule above: "" == claims.get("iss") for a token with
    # no issuer at all, so a half-written provider must not be a record.
    with pytest.raises(InvalidValueError):
        provider(issuer="", client_id="")


def test_a_provider_asks_for_openid_or_it_gets_no_id_token() -> None:
    with pytest.raises(InvalidValueError):
        provider(scopes=("email", "profile"))
    assert provider(scopes=["openid", "email"]).scopes == ("openid", "email")


def test_a_provider_authenticates_one_of_the_two_documented_ways() -> None:
    with pytest.raises(InvalidValueError):
        provider(token_endpoint_auth="private_key_jwt")
    assert provider(token_endpoint_auth="client_secret_post").token_endpoint_auth == (
        "client_secret_post"
    )


def test_a_groups_claim_names_a_claim_or_is_left_out() -> None:
    with pytest.raises(InvalidValueError):
        provider(groups_claim="")
    assert provider(groups_claim=None).groups_claim is None


@pytest.mark.parametrize(
    "issuer",
    [
        GOOGLE_ISSUER,
        "https://accounts.google.com/",
        "https://accounts.google.com.",
        "HTTPS://Accounts.Google.COM.:443/",
        "https://accounts.google.com:443",
        "accounts.google.com",
        "ACCOUNTS.GOOGLE.COM.",
    ],
)
def test_google_is_recognised_however_its_issuer_is_spelt(issuer: str) -> None:
    # Whether this is Google decides whether hd is read, whether
    # email_verified is believed, and whether email_domain and everyone are
    # refused. A spelling that read as "not Google" would drop them all at once.
    assert is_google_issuer(issuer)
    assert provider(issuer=issuer).is_google


@pytest.mark.parametrize(
    "issuer",
    [
        "https://example.okta.com/oauth2/default",
        "https://accounts.google.com.evil.test",
        "https://evil.test/accounts.google.com",
        "https://accounts.google.com@evil.test",
        "https://accountsxgoogle.com",
        "https://[::1",
        "",
        42,
        None,
    ],
)
def test_nothing_else_passes_for_google(issuer: object) -> None:
    assert not is_google_issuer(issuer)


def test_a_provider_that_is_not_google_says_so() -> None:
    assert not provider().is_google


def test_a_config_serves_the_redirect_uri_and_the_cookie_rules() -> None:
    signin = config()
    assert signin.redirect_uri("okta") == "https://robinauts.example.com/auth/callback/okta"
    assert signin.secure
    assert not config(public_url="http://localhost:8000").secure
    assert signin.session_life == timedelta(hours=DEFAULT_SESSION_HOURS)


def test_a_config_refuses_an_unknown_provider_with_the_spec_code() -> None:
    with pytest.raises(UnknownProviderError) as raised:
        config().provider("google")
    assert raised.value.code is SignInErrorCode.UNKNOWN_PROVIDER
    assert config().provider("okta").id == "okta"


def test_a_config_keeps_its_providers_out_of_reach_of_a_caller() -> None:
    providers = {"okta": provider()}
    signin = config(providers=providers)
    providers["google"] = provider(id="google")
    assert "google" not in signin.providers
    with pytest.raises(TypeError):
        signin.providers["other"] = provider()  # type: ignore[index]


@pytest.mark.parametrize("hours", [0, -1, MAX_SESSION_HOURS + 1])
def test_a_session_life_stays_within_its_bounds(hours: float) -> None:
    with pytest.raises(InvalidValueError):
        config(session_hours=hours)


def test_a_config_needs_its_public_url() -> None:
    with pytest.raises(InvalidValueError):
        config(public_url="")


def test_a_user_is_keyed_by_provider_and_subject() -> None:
    user = User(id=uuid.uuid4(), provider="okta", subject="00u1", name="Ada")
    assert user.key == ("okta", "00u1")
    with pytest.raises(InvalidValueError):
        User(id=uuid.uuid4(), provider="okta", subject="")


def test_a_session_is_over_at_its_expiry_and_not_before() -> None:
    session = Session(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        created_at=NOW,
        expires_at=NOW + timedelta(hours=12),
    )
    assert not session.has_expired(NOW + timedelta(hours=12) - timedelta(seconds=1))
    assert session.has_expired(NOW + timedelta(hours=12))


@pytest.mark.parametrize("name", ["provider", "nonce", "verifier"])
def test_a_pending_login_needs_what_proves_the_callback_is_its_own(name: str) -> None:
    # An empty nonce would compare equal to a token carrying no nonce, and an
    # empty verifier hands PKCE away: neither is a sign-in in progress.
    with pytest.raises(InvalidValueError):
        pending(**{name: ""})


@pytest.mark.parametrize("build", [session, pending], ids=["session", "pending"])
def test_an_expiry_is_never_naive_and_is_never_compared_with_a_naive_now(
    build: Callable[..., Session | PendingLogin],
) -> None:
    naive = NOW.replace(tzinfo=None)
    with pytest.raises(InvalidValueError):
        build(expires_at=naive)
    with pytest.raises(InvalidValueError):
        build(created_at=naive)
    with pytest.raises(InvalidValueError):
        build().has_expired(naive)


def test_a_user_records_an_aware_creation_time_or_none() -> None:
    assert User(id=uuid.uuid4(), provider="okta", subject="00u1").created_at is None
    with pytest.raises(InvalidValueError):
        User(id=uuid.uuid4(), provider="okta", subject="00u1", created_at=NOW.replace(tzinfo=None))


def test_a_pending_login_is_over_at_its_expiry_and_not_before() -> None:
    pending = PendingLogin(
        provider="okta",
        nonce="n",
        verifier="v",
        created_at=NOW,
        expires_at=NOW + timedelta(minutes=10),
        return_to="/c/1",
    )
    assert not pending.has_expired(NOW + timedelta(minutes=9))
    assert pending.has_expired(NOW + timedelta(minutes=10))
