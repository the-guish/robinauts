# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The allow list: who the matchers let in, and who they must not."""

import pytest

from robinauts.legacy.core import is_allowed, matches, verified_email
from robinauts.legacy.core.allow import ascii_lower
from robinauts.legacy.domain import AllowEntry, Identity, InvalidValueError, Matcher


def identity(**changes: object) -> Identity:
    fields: dict[str, object] = {
        "provider": "okta",
        "subject": "00u1",
        "name": "Ada",
        "email": "ada@example.com",
        "email_verified": True,
    }
    fields.update(changes)
    return Identity(**fields)  # type: ignore[arg-type]


# --- an entry only ever speaks for its own provider ------------------------


def test_an_entry_never_matches_another_providers_identity() -> None:
    entry = AllowEntry("okta", Matcher.EVERYONE)
    assert not matches(entry, identity(provider="local"))
    assert matches(entry, identity())


# --- everyone --------------------------------------------------------------


def test_everyone_takes_anyone_the_provider_authenticated() -> None:
    entry = AllowEntry("okta", Matcher.EVERYONE)
    assert matches(entry, identity(email=None, email_verified=False, name=None))


# --- subject ---------------------------------------------------------------


def test_subject_is_compared_exactly() -> None:
    entry = AllowEntry("okta", Matcher.SUBJECT, "00u1")
    assert matches(entry, identity(subject="00u1"))
    assert not matches(entry, identity(subject="00U1"))
    assert not matches(entry, identity(subject="00u2"))


def test_subject_does_not_need_a_verified_email() -> None:
    entry = AllowEntry("okta", Matcher.SUBJECT, "00u1")
    assert matches(entry, identity(email=None, email_verified=False))


# --- email -----------------------------------------------------------------


def test_email_ignores_case_on_both_sides() -> None:
    entry = AllowEntry("okta", Matcher.EMAIL, "Ada@Example.COM")
    assert matches(entry, identity(email="ADA@example.com"))


def test_email_matches_nobody_the_provider_did_not_verify() -> None:
    entry = AllowEntry("okta", Matcher.EMAIL, "ada@example.com")
    assert not matches(entry, identity(email_verified=False))
    assert not matches(entry, identity(email=None))
    assert not matches(entry, identity(email=""))


def test_email_does_not_match_another_address() -> None:
    entry = AllowEntry("okta", Matcher.EMAIL, "ada@example.com")
    assert not matches(entry, identity(email="ada@example.com.evil.test"))
    assert not matches(entry, identity(email="bob@example.com"))


# --- email_domain ----------------------------------------------------------


def test_email_domain_takes_the_part_after_the_last_at_sign() -> None:
    entry = AllowEntry("okta", Matcher.EMAIL_DOMAIN, "Example.com")
    assert matches(entry, identity(email="ADA@EXAMPLE.COM"))
    assert matches(entry, identity(email='"odd@name"@example.com'))
    assert not matches(entry, identity(email="ada@example.com.evil.test"))
    assert not matches(entry, identity(email="ada@sub.example.com"))


def test_email_domain_matches_nobody_unverified() -> None:
    entry = AllowEntry("okta", Matcher.EMAIL_DOMAIN, "example.com")
    assert not matches(entry, identity(email_verified=False))


# --- hosted_domain ---------------------------------------------------------


def test_hosted_domain_reads_googles_hd_claim_without_regard_to_case() -> None:
    entry = AllowEntry("google", Matcher.HOSTED_DOMAIN, "Example.com")
    assert matches(entry, identity(provider="google", hosted_domain="EXAMPLE.com"))
    assert not matches(entry, identity(provider="google", hosted_domain="other.example"))
    assert not matches(entry, identity(provider="google", hosted_domain=None))


def test_hosted_domain_does_not_fall_back_to_the_address() -> None:
    entry = AllowEntry("google", Matcher.HOSTED_DOMAIN, "example.com")
    assert not matches(entry, identity(provider="google", email="ada@example.com"))


# --- group -----------------------------------------------------------------


def test_group_is_compared_exactly_and_never_folded() -> None:
    entry = AllowEntry("okta", Matcher.GROUP, "robinauts-users")
    assert matches(entry, identity(groups=frozenset({"other", "robinauts-users"})))
    assert not matches(entry, identity(groups=frozenset({"Robinauts-Users"})))
    assert not matches(entry, identity(groups=frozenset()))


# --- the list as a whole ---------------------------------------------------


def test_an_empty_allow_list_lets_nobody_in() -> None:
    assert not is_allowed(identity(), [])


def test_one_entry_of_several_is_enough() -> None:
    allow = [
        AllowEntry("local", Matcher.EVERYONE),
        AllowEntry("okta", Matcher.GROUP, "robinauts-users"),
        AllowEntry("okta", Matcher.SUBJECT, "00u1"),
    ]
    assert is_allowed(identity(subject="00u1"), allow)
    assert not is_allowed(identity(subject="00u9"), allow)


# --- the verified address itself -------------------------------------------


@pytest.mark.parametrize(
    ("email", "verified", "expected"),
    [
        ("Ada@Example.com", True, "ada@example.com"),
        ("ada@example.com", False, None),
        (None, True, None),
        ("", True, None),
        ("not-an-address", True, None),
    ],
)
def test_the_verified_address_is_lower_cased_or_nothing(
    email: str | None, verified: bool, expected: str | None
) -> None:
    assert verified_email(identity(email=email, email_verified=verified)) == expected


# --- case is ignored inside ASCII, and only there -------------------------

KELVIN = "\u212a"
"""U+212A KELVIN SIGN: ``.lower()`` turns it into a plain ``k``."""


def test_a_letter_that_lowers_onto_ascii_does_not_match_the_ascii_one() -> None:
    assert KELVIN.lower() == "k"  # the reason this rule exists
    entry = AllowEntry("okta", Matcher.EMAIL, "kelvin@example.com")
    assert not matches(entry, identity(email=f"{KELVIN}elvin@example.com"))
    domain = AllowEntry("okta", Matcher.EMAIL_DOMAIN, "kelvin.example")
    assert not matches(domain, identity(email=f"ada@{KELVIN}elvin.example"))
    hosted = AllowEntry("google", Matcher.HOSTED_DOMAIN, "kelvin.example")
    assert not matches(hosted, identity(provider="google", hosted_domain=f"{KELVIN}elvin.example"))


def test_a_value_outside_ascii_matches_nobody_either_way() -> None:
    written = AllowEntry("okta", Matcher.EMAIL, f"{KELVIN}elvin@example.com")
    assert not matches(written, identity(email=f"{KELVIN}elvin@example.com"))
    assert not matches(written, identity(email="kelvin@example.com"))


def test_an_address_outside_ascii_is_not_one_this_compares() -> None:
    assert verified_email(identity(email="ada@b\u00fccher.example")) is None
    assert ascii_lower("Ada@Example.com") == "ada@example.com"
    assert ascii_lower(f"{KELVIN}") is None
    assert ascii_lower(None) is None


def test_a_matcher_that_is_no_matcher_refuses_rather_than_guessing() -> None:
    # AllowEntry refuses to be built with one; were one to arrive another
    # way, matching says so instead of falling through to "no".
    entry = AllowEntry("okta", Matcher.GROUP, "g")
    object.__setattr__(entry, "matcher", "group")
    with pytest.raises(InvalidValueError):
        matches(entry, identity())
