# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The ID token: reading it, and refusing one that is not this sign-in's."""

import base64
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from robinauts.legacy.core import (
    CLOCK_SKEW_SECONDS,
    MAX_ID_TOKEN_CHARS,
    accepted_issuers,
    check_id_token_claims,
    check_published_issuer,
    decode_id_token,
    identity_from_claims,
    identity_from_id_token,
)
from robinauts.legacy.domain import (
    InvalidIdTokenError,
    InvalidValueError,
    ProviderConfig,
    ProviderUnavailableError,
    RobinautsError,
    SignInErrorCode,
)

OKTA = ProviderConfig(
    id="okta",
    title="Okta",
    issuer="https://example.okta.com/oauth2/default",
    client_id="cid",
    client_secret_env="ROBINAUTS_OKTA_SECRET",
    groups_claim="groups",
)
GOOGLE = ProviderConfig(
    id="google",
    title="Google",
    issuer="https://accounts.google.com",
    client_id="cid.apps.googleusercontent.com",
    client_secret_env="ROBINAUTS_GOOGLE_SECRET",
)

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
ISSUED = NOW.timestamp() - 30
EXPIRES = NOW.timestamp() + 3600
NONCE = "the-nonce"


def claims(**changes: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "iss": OKTA.issuer,
        "aud": OKTA.client_id,
        "sub": "00u1",
        "iat": ISSUED,
        "exp": EXPIRES,
        "nonce": NONCE,
    }
    payload.update(changes)
    return {key: value for key, value in payload.items() if value is not ...}


def check(payload: dict[str, Any], provider: ProviderConfig = OKTA, **changes: Any) -> None:
    changes.setdefault("nonce", NONCE)
    changes.setdefault("now", NOW)
    check_id_token_claims(payload, provider, **changes)


def refused(payload: dict[str, Any], provider: ProviderConfig = OKTA, **changes: Any) -> str:
    with pytest.raises(InvalidIdTokenError) as raised:
        check(payload, provider, **changes)
    assert raised.value.code is SignInErrorCode.INVALID_ID_TOKEN
    return raised.value.detail


def jwt(payload: dict[str, Any]) -> str:
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"header.{body}.signature"


# --- decoding --------------------------------------------------------------


def test_the_claims_are_the_middle_part_of_the_token() -> None:
    assert decode_id_token(jwt(claims()))["sub"] == "00u1"


def test_a_payload_whose_padding_was_stripped_still_reads() -> None:
    for subject in ("a", "ab", "abc", "abcd"):
        assert decode_id_token(jwt(claims(sub=subject)))["sub"] == subject


@pytest.mark.parametrize(
    "token",
    [
        "",
        "one.two",
        "one.two.three.four",
        "header..signature",
        "header.!!!not-base64!!!.signature",
        f"header.{base64.urlsafe_b64encode(b'[1, 2]').decode()}.signature",
        f"header.{base64.urlsafe_b64encode(b'not json').decode()}.signature",
    ],
)
def test_anything_that_is_not_a_jwt_with_an_object_is_refused(token: str) -> None:
    with pytest.raises(InvalidIdTokenError):
        decode_id_token(token)


# --- iss -------------------------------------------------------------------


def test_the_issuer_must_be_the_configured_one() -> None:
    check(claims())
    assert "iss" in refused(claims(iss="https://evil.test"))
    assert "iss" in refused(claims(iss=...))


def test_the_issuer_is_compared_exactly_not_as_a_url() -> None:
    assert "iss" in refused(claims(iss=OKTA.issuer + "/"))


def test_only_the_configured_issuer_is_accepted_whatever_discovery_says() -> None:
    assert accepted_issuers(OKTA) == {OKTA.issuer}
    # What a provider publishes about itself does not widen the set: a token
    # from another issuer is refused even if discovery named that issuer.
    assert "iss" in refused(claims(iss="https://evil.test"))


@pytest.mark.parametrize(
    "published",
    [
        "https://example.okta.com/oauth2/default",
        "https://example.okta.com/oauth2/default/",
        "https://EXAMPLE.okta.com/oauth2/default",
        "https://example.okta.com:443/oauth2/default",
    ],
)
def test_discovery_may_spell_the_configured_issuer_any_way(published: str) -> None:
    check_published_issuer(OKTA, published)


@pytest.mark.parametrize(
    "published",
    [
        "https://evil.test/oauth2/default",
        "https://example.okta.com/oauth2/other",
        "https://example.okta.com",
        "accounts.google.com",
        "not a url",
        "",
        None,
        42,
        ["https://example.okta.com/oauth2/default"],
    ],
)
def test_an_issuer_discovery_names_that_is_not_the_configured_one_is_unusable(
    published: object,
) -> None:
    with pytest.raises(ProviderUnavailableError) as raised:
        check_published_issuer(OKTA, published)
    assert raised.value.code is SignInErrorCode.PROVIDER_UNAVAILABLE


def test_googles_bare_form_is_accepted_as_a_token_issuer_but_not_from_discovery() -> None:
    assert "accounts.google.com" in accepted_issuers(GOOGLE)
    with pytest.raises(ProviderUnavailableError):
        check_published_issuer(GOOGLE, "accounts.google.com")
    check_published_issuer(GOOGLE, "https://accounts.google.com")


def test_google_may_name_itself_with_or_without_the_scheme() -> None:
    google = claims(iss="accounts.google.com", aud=GOOGLE.client_id)
    check(google, GOOGLE)
    check(claims(iss="https://accounts.google.com", aud=GOOGLE.client_id), GOOGLE)
    assert accepted_issuers(GOOGLE) == {"https://accounts.google.com", "accounts.google.com"}
    # The bare form is Google's alone.
    assert "accounts.google.com" not in accepted_issuers(OKTA)


def test_an_issuer_that_is_not_a_string_is_refused() -> None:
    assert "iss" in refused(claims(iss=[OKTA.issuer]))


# --- aud and azp -----------------------------------------------------------


def test_the_audience_may_be_a_string_or_a_list_holding_our_client() -> None:
    check(claims(aud="cid"))
    check(claims(aud=["cid"]))
    assert "aud" in refused(claims(aud="other"))
    assert "aud" in refused(claims(aud=["other"]))
    assert "aud" in refused(claims(aud=...))
    assert "aud" in refused(claims(aud={"client": "cid"}))


def test_several_audiences_need_azp_to_name_us() -> None:
    assert "azp" in refused(claims(aud=["cid", "other"]))
    assert "azp" in refused(claims(aud=["cid", "other"], azp="other"))
    check(claims(aud=["cid", "other"], azp="cid"))


def test_an_azp_that_is_there_is_honoured_even_with_one_audience() -> None:
    check(claims(azp="cid"))
    assert "azp" in refused(claims(azp="other"))


# --- exp and iat, with a minute of skew ------------------------------------


def test_an_expired_token_is_refused_a_minute_after_it_expired() -> None:
    assert CLOCK_SKEW_SECONDS == 60.0
    exp = NOW.timestamp()
    check(claims(exp=exp), now=NOW + timedelta(seconds=59))
    assert "exp" in refused(claims(exp=exp), now=NOW + timedelta(seconds=60))
    assert "exp" in refused(claims(exp=exp), now=NOW + timedelta(hours=1))


def test_a_token_issued_in_the_future_is_refused_beyond_the_skew() -> None:
    check(claims(iat=NOW.timestamp() + 60))
    assert "iat" in refused(claims(iat=NOW.timestamp() + 61))


@pytest.mark.parametrize("value", [..., None, "1758456000", True, [1]])
def test_a_time_that_is_not_a_number_is_no_time_at_all(value: Any) -> None:
    assert "exp" in refused(claims(exp=value))
    assert "iat" in refused(claims(iat=value))


def test_now_must_carry_a_time_zone() -> None:
    with pytest.raises(InvalidValueError):
        check(claims(), now=NOW.replace(tzinfo=None))


# --- nonce and sub ---------------------------------------------------------


def test_the_nonce_must_be_this_sign_ins() -> None:
    assert "nonce" in refused(claims(nonce="another"))
    assert "nonce" in refused(claims(nonce=...))
    assert "nonce" in refused(claims(nonce=123))


def test_an_empty_expected_nonce_is_a_mistake_and_never_a_match() -> None:
    with pytest.raises(InvalidValueError):
        check(claims(nonce=""), nonce="")
    with pytest.raises(InvalidValueError):
        check(claims(), nonce="")


def test_a_nonce_outside_ascii_is_compared_rather_than_crashing() -> None:
    check(claims(nonce="nonce-é"), nonce="nonce-é")
    assert "nonce" in refused(claims(nonce="nonce-é"), nonce="nonce-e")


def test_a_token_without_a_subject_is_refused() -> None:
    assert "sub" in refused(claims(sub=...))
    assert "sub" in refused(claims(sub=""))
    assert "sub" in refused(claims(sub=12345))


# --- the identity the claims describe --------------------------------------


def test_an_identity_carries_what_the_provider_said() -> None:
    identity = identity_from_claims(
        claims(name="Ada", email="ada@example.com", email_verified=True), OKTA
    )
    assert identity.provider == "okta"
    assert identity.subject == "00u1"
    assert identity.name == "Ada"
    assert identity.email == "ada@example.com"
    assert identity.email_verified


def test_an_absent_name_or_address_is_none_rather_than_empty() -> None:
    identity = identity_from_claims(claims(name="", email=""), OKTA)
    assert identity.name is None
    assert identity.email is None
    assert not identity.email_verified


def test_email_verified_is_read_as_the_word_some_providers_send() -> None:
    assert identity_from_claims(
        claims(email="a@b.test", email_verified="true"), OKTA
    ).email_verified
    assert not identity_from_claims(
        claims(email="a@b.test", email_verified="yes"), OKTA
    ).email_verified


@pytest.mark.parametrize("value", [1, 1.0, "1", "yes", "TRUE ", None, [True], {"v": True}])
def test_nothing_but_true_or_the_word_counts_as_a_verified_address(value: Any) -> None:
    verified = identity_from_claims(claims(email="a@b.test", email_verified=value), OKTA)
    assert verified.email_verified is (value == "TRUE ")


def test_the_groups_claim_is_read_as_a_list_or_as_one_name() -> None:
    assert identity_from_claims(claims(groups=["a", "b"]), OKTA).groups == frozenset({"a", "b"})
    assert identity_from_claims(claims(groups="a"), OKTA).groups == frozenset({"a"})
    assert identity_from_claims(claims(groups=[1, "a", None]), OKTA).groups == frozenset({"a"})
    assert identity_from_claims(claims(groups={"a": 1}), OKTA).groups == frozenset()
    assert identity_from_claims(claims(), OKTA).groups == frozenset()


def test_a_provider_with_no_groups_claim_reads_none() -> None:
    no_groups = ProviderConfig(
        id="okta",
        title="Okta",
        issuer=OKTA.issuer,
        client_id="cid",
        client_secret_env="ROBINAUTS_OKTA_SECRET",
    )
    assert identity_from_claims(claims(groups=["a"]), no_groups).groups == frozenset()


def test_googles_hosted_domain_is_read_only_from_google() -> None:
    google = identity_from_claims(claims(hd="example.com"), GOOGLE)
    assert google.hosted_domain == "example.com"
    assert identity_from_claims(claims(hd="example.com"), OKTA).hosted_domain is None
    assert identity_from_claims(claims(hd=42), GOOGLE).hosted_domain is None


@pytest.mark.parametrize(
    ("email", "hosted_domain", "expected"),
    [
        ("ada@example.com", "example.com", True),
        ("ada@gmail.com", None, True),
        ("ada@googlemail.com", None, True),
        ("ada@GMAIL.com", None, True),
        ("ada@example.com", None, False),
        ("ada@gmail.com.evil.test", None, False),
    ],
)
def test_google_is_believed_about_an_address_only_with_hd_or_at_gmail(
    email: str, hosted_domain: str | None, expected: bool
) -> None:
    payload = claims(email=email, email_verified=True)
    if hosted_domain is not None:
        payload["hd"] = hosted_domain
    assert identity_from_claims(payload, GOOGLE).email_verified is expected
    # Another provider's word about an address it verified is taken as it is.
    assert identity_from_claims(payload, OKTA).email_verified is True


def test_an_unverified_google_address_stays_unverified_even_with_hd() -> None:
    payload = claims(email="ada@example.com", email_verified=False, hd="example.com")
    assert not identity_from_claims(payload, GOOGLE).email_verified


def test_an_identity_cannot_be_read_from_claims_without_a_subject() -> None:
    with pytest.raises(InvalidIdTokenError):
        identity_from_claims(claims(sub=...), OKTA)


# --- the whole of it -------------------------------------------------------


def test_a_token_is_read_checked_and_turned_into_an_identity_in_one_call() -> None:
    identity = identity_from_id_token(
        jwt(claims(name="Ada", groups=["robinauts-users"])), OKTA, nonce=NONCE, now=NOW
    )
    assert identity.subject == "00u1"
    assert identity.groups == frozenset({"robinauts-users"})
    with pytest.raises(InvalidIdTokenError):
        identity_from_id_token(jwt(claims()), OKTA, nonce="another", now=NOW)


# --- what no claim may do: escape as something other than a refusal ---------


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_json_that_is_not_json_is_refused_before_a_claim_is_ever_read(constant: str) -> None:
    # json.loads accepts these as an extension; RFC 8259 has no such thing,
    # and a NaN exp compares false against every clock -- a token that never
    # expires. They are refused whole, so no claim can hold one.
    raw = b'{"iss": "x", "exp": ' + constant.encode() + b"}"
    token = "h." + base64.urlsafe_b64encode(raw).decode().rstrip("=") + ".s"
    with pytest.raises(InvalidIdTokenError):
        decode_id_token(token)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_a_time_that_is_not_finite_expires_nothing_and_is_refused(value: float) -> None:
    assert "exp" in refused(claims(exp=value))
    assert "iat" in refused(claims(iat=value))


@pytest.mark.parametrize("value", [10**400, -(10**400), 10**4000])
def test_a_time_too_large_for_a_clock_is_refused_rather_than_raising(value: int) -> None:
    assert "exp" in refused(claims(exp=value))
    assert "iat" in refused(claims(iat=value))


def test_a_lone_surrogate_in_the_nonce_is_refused_rather_than_raising() -> None:
    # JSON can hold "\ud800"; UTF-8 cannot encode it. Refusing to encode it
    # must not turn into a UnicodeEncodeError escaping the claim check.
    assert "nonce" in refused(claims(nonce="\ud800"))
    check(claims(nonce="n-\ud800"), nonce="n-\ud800")


HOSTILE: list[Any] = [
    None,
    True,
    False,
    0,
    1,
    -1,
    1.5,
    10**400,
    float("nan"),
    float("inf"),
    "",
    " ",
    "\ud800",
    "\x00",
    "x" * 5000,
    [],
    ["\ud800"],
    [{"a": 1}],
    {},
    {"a": ["\ud800"]},
    [[["deep"]]],
]
"""Values a provider, or whoever reached the token endpoint, may have sent."""

CLAIMS = [
    "iss",
    "aud",
    "azp",
    "exp",
    "iat",
    "nonce",
    "sub",
    "email",
    "email_verified",
    "hd",
    "name",
    "groups",
]


@pytest.mark.parametrize("claim", CLAIMS)
@pytest.mark.parametrize("provider", [OKTA, GOOGLE], ids=["okta", "google"])
def test_no_claim_of_any_shape_escapes_as_anything_but_a_refusal(
    claim: str, provider: ProviderConfig
) -> None:
    for value in HOSTILE:
        payload = claims(aud=provider.client_id, iss=provider.issuer)
        payload[claim] = value
        try:
            check_id_token_claims(payload, provider, nonce=NONCE, now=NOW)
        except InvalidIdTokenError:
            continue
        except RobinautsError as exc:  # pragma: no cover -- a wrong kind of refusal
            raise AssertionError(f"{claim}={value!r} raised {exc!r}") from exc
        # It passed the checks, so reading an identity out of it must work.
        identity = identity_from_claims(payload, provider)
        assert identity.subject
        assert identity.provider == provider.id


@pytest.mark.parametrize("claim", CLAIMS)
@pytest.mark.parametrize("provider", [OKTA, GOOGLE], ids=["okta", "google"])
def test_reading_an_identity_from_any_claims_refuses_rather_than_raises(
    claim: str, provider: ProviderConfig
) -> None:
    for value in HOSTILE:
        payload = claims()
        payload[claim] = value
        try:
            identity = identity_from_claims(payload, provider)
        except InvalidIdTokenError:
            continue
        assert isinstance(identity.email_verified, bool)
        assert identity.name is None or identity.name
        assert all(isinstance(group, str) for group in identity.groups)


@pytest.mark.parametrize("piece", ["", "x", "!!", "e30", "bnVsbA", "W10"])
def test_a_payload_of_any_shape_is_refused_rather_than_raising(piece: str) -> None:
    try:
        decode_id_token(f"h.{piece}.s")
    except InvalidIdTokenError:
        return
    # An object decoded: the checks below it must still refuse it.
    assert "iss" in refused(decode_id_token(f"h.{piece}.s"))


def test_a_message_never_repeats_a_whole_claim() -> None:
    detail = refused(claims(iss="x" * 100_000))
    assert len(detail) < 300


def test_a_trailing_dot_in_googles_issuer_does_not_switch_googles_rules_off() -> None:
    # ``accounts.google.com.`` is the same host as ``accounts.google.com``.
    # Were it read as another provider, hd would not be read and Google's
    # word about an address would be believed on its own.
    dotted = ProviderConfig(
        id="google",
        title="Google",
        issuer="https://accounts.google.com.",
        client_id=GOOGLE.client_id,
        client_secret_env="ROBINAUTS_GOOGLE_SECRET",
    )
    assert dotted.is_google
    payload = claims(email="ada@example.com", email_verified=True)
    assert not identity_from_claims(payload, dotted).email_verified
    payload["hd"] = "example.com"
    read = identity_from_claims(payload, dotted)
    assert read.hosted_domain == "example.com"
    assert read.email_verified


# --- the encoding is checked, not guessed at -------------------------------

VALID = base64.urlsafe_b64encode(json.dumps(claims()).encode()).rstrip(b"=").decode()
"""A payload of the right length, so that what follows fails on its characters."""


@pytest.mark.parametrize(
    ("piece", "why"),
    [
        (VALID + "*", "a character outside the alphabet, at a valid length"),
        (VALID[:-1] + "+", "base64's + is not base64url's -"),
        (VALID[:-1] + "/", "base64's / is not base64url's _"),
        (VALID[:-2] + " " + VALID[-1], "a space, which the decoder would drop"),
        (VALID[:-2] + "\n" + VALID[-1], "a newline, which the decoder would drop"),
        (VALID + "===", "more padding than base64 has"),
        ("e30==", "more padding than the length needs"),
        ("e30=A", "padding in the middle of a part"),
        ("=", "padding and nothing else"),
        ("", "no payload at all"),
        ("A", "a length that decodes to nothing"),
    ],
)
def test_a_payload_that_is_not_base64url_is_refused_rather_than_trimmed(
    piece: str, why: str
) -> None:
    # urlsafe_b64decode drops what is not in its alphabet, so two spellings
    # of one token would decode alike: one read by a check, another by the
    # code that trusts it.
    with pytest.raises(InvalidIdTokenError):
        decode_id_token(f"header.{piece}.signature")


def test_a_correctly_padded_payload_is_still_read() -> None:
    padded = base64.urlsafe_b64encode(json.dumps(claims()).encode()).decode()
    assert decode_id_token(f"header.{padded}.signature")["sub"] == "00u1"


@pytest.mark.parametrize("token", ["", "one", "one.two", "one.two.three.four", "..", "a.b.c.d.e"])
def test_a_token_is_three_parts_and_nothing_else(token: str) -> None:
    with pytest.raises(InvalidIdTokenError):
        decode_id_token(token)


def test_a_token_no_provider_would_send_is_refused_unread() -> None:
    huge = "h." + "A" * MAX_ID_TOKEN_CHARS + ".s"
    with pytest.raises(InvalidIdTokenError) as raised:
        decode_id_token(huge)
    assert str(MAX_ID_TOKEN_CHARS) in raised.value.detail
    assert len(raised.value.detail) < 200


@pytest.mark.parametrize("token", [None, 42, b"h.e30.s", ["h", "e30", "s"]])
def test_a_token_that_is_not_text_is_refused(token: object) -> None:
    with pytest.raises(InvalidIdTokenError):
        decode_id_token(token)  # type: ignore[arg-type]


def test_a_refusal_of_discovery_names_both_issuers_and_no_traceback() -> None:
    with pytest.raises(ProviderUnavailableError) as raised:
        check_published_issuer(OKTA, "not a url")
    detail = raised.value.detail
    assert "'not a url'" in detail
    assert OKTA.issuer in detail
    assert "Error" not in detail
    assert "(" not in detail
