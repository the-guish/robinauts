# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""What is kept of a secret, what one must be, and the challenge from a verifier."""

import base64
import hashlib

import pytest

from robinauts.legacy.core import (
    MAX_PKCE_CHARS,
    MAX_SECRET_CHARS,
    MIN_SECRET_CHARS,
    UNRESERVED,
    checked_secret,
    is_secret_shaped,
    pkce_challenge,
    same_secret,
    secret_hash,
)
from robinauts.legacy.domain import InvalidValueError


def test_a_secret_is_kept_as_its_sha256_in_hex() -> None:
    assert secret_hash("abc") == (
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )
    assert len(secret_hash("")) == 64


def test_the_hash_is_the_same_for_the_same_secret_and_not_for_another() -> None:
    assert secret_hash("s3cret") == secret_hash("s3cret")
    assert secret_hash("s3cret") != secret_hash("s3crey")


def test_a_secret_outside_ascii_is_hashed_rather_than_refused() -> None:
    assert len(secret_hash("café \U0001f512")) == 64


def test_the_pkce_challenge_is_the_one_rfc_7636_shows() -> None:
    # RFC 7636 appendix B.
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    assert pkce_challenge(verifier) == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def test_the_challenge_is_base64url_without_padding() -> None:
    challenge = pkce_challenge("a-verifier-of-no-consequence")
    assert "=" not in challenge
    assert "+" not in challenge
    assert "/" not in challenge
    digest = base64.urlsafe_b64decode(challenge + "==")
    assert digest == hashlib.sha256(b"a-verifier-of-no-consequence").digest()


def test_two_secrets_are_the_same_or_they_are_not() -> None:
    assert same_secret("s3cret", "s3cret")
    assert not same_secret("s3cret", "s3crey")
    assert not same_secret("s3cret", "s3cret ")


@pytest.mark.parametrize(
    ("one", "other"),
    [("", ""), ("", "s"), ("s", ""), ("x" * (MAX_SECRET_CHARS + 1),) * 2],
)
def test_what_is_not_shaped_like_a_secret_is_never_the_same_as_anything(
    one: str, other: str
) -> None:
    # An empty cookie must not equal an empty query parameter, and nothing
    # oversized is compared at all.
    assert not same_secret(one, other)


def test_a_secret_outside_ascii_is_compared_rather_than_raising() -> None:
    # A claim or a cookie may hold a lone surrogate, which UTF-8 refuses to
    # encode; that must be a mismatch, not an error escaping a sign-in.
    assert not same_secret("café" + "x" * 40, "\ud800" + "x" * 43)
    assert same_secret("café", "café")


@pytest.mark.parametrize("secret", ["s", "s" * MAX_SECRET_CHARS, "café", "K" * 43])
def test_anything_of_a_workable_length_is_worth_looking_up(secret: str) -> None:
    assert is_secret_shaped(secret)


@pytest.mark.parametrize("secret", ["", "s" * (MAX_SECRET_CHARS + 1), None, 7, b"ss"])
def test_nothing_else_is(secret: object) -> None:
    assert not is_secret_shaped(secret)


def test_a_secret_a_source_gave_is_handed_back_when_it_is_one() -> None:
    made = "a" * MIN_SECRET_CHARS
    assert checked_secret(made, "state") is made
    assert checked_secret("-_" + "a" * 41, "state") == "-_" + "a" * 41
    assert checked_secret("a" * 43 + ".~", "PKCE verifier", alphabet=UNRESERVED)


@pytest.mark.parametrize(
    "made",
    [
        "",
        "short",
        "a" * (MIN_SECRET_CHARS - 1),
        "a" * (MAX_SECRET_CHARS + 1),
        "a" * 42 + "+",
        "a" * 42 + "/",
        "a" * 42 + "=",
        "a" * 42 + " ",
        "a" * 42 + "é",
        "a" * 42 + ".",
    ],
)
def test_a_source_that_gave_something_else_stops_the_deployment(made: str) -> None:
    with pytest.raises(InvalidValueError):
        checked_secret(made, "state")


def test_a_verifier_is_held_to_the_range_rfc_7636_gives() -> None:
    assert checked_secret("a" * MAX_PKCE_CHARS, "PKCE verifier", most=MAX_PKCE_CHARS)
    with pytest.raises(InvalidValueError, match="128"):
        checked_secret("a" * (MAX_PKCE_CHARS + 1), "PKCE verifier", most=MAX_PKCE_CHARS)


@pytest.mark.parametrize("made", [None, 7, b"a" * 43, ["a" * 43]])
def test_a_source_that_gave_something_that_is_not_text_stops_it_too(made: object) -> None:
    with pytest.raises(InvalidValueError):
        checked_secret(made, "state")
