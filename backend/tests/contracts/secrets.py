# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""What every ``SecretSource`` must give: length, alphabet, and never twice.

Subclass ``SecretSourceContract`` and override ``new_source``. What cannot be
tested is the one thing that matters most -- that a secret is unguessable --
so what is tested is everything that would make it guessable by accident: too
few characters for 256 bits, an alphabet that is not the one a cookie and a
URL carry, a source that repeats itself. A deterministic source written for a
test passes these; a stub returning ``"secret"`` does not.
"""

from __future__ import annotations

from robinauts.legacy.core import (
    MAX_PKCE_CHARS,
    MAX_SECRET_CHARS,
    MIN_SECRET_CHARS,
    UNRESERVED,
    URL_SAFE,
)
from robinauts.legacy.ports import SecretSource

DRAWS = 200
"""How many of each are taken when looking for a repeat."""


class SecretSourceContract:
    """Subclass this and override ``new_source``."""

    def new_source(self) -> SecretSource:
        """A source to draw from."""
        raise NotImplementedError("a SecretSourceContract subclass overrides `new_source`")

    def test_a_secret_is_long_enough_to_carry_256_bits(self) -> None:
        source = self.new_source()

        assert len(source.secret()) >= MIN_SECRET_CHARS

    def test_a_secret_is_short_enough_to_be_looked_up(self) -> None:
        # Past this nothing looks one up, so a source giving more would open
        # sessions that every later request fails to recognise.
        source = self.new_source()

        assert len(source.secret()) <= MAX_SECRET_CHARS

    def test_a_secret_is_spelt_with_what_a_url_and_a_cookie_carry(self) -> None:
        source = self.new_source()

        secret = source.secret()

        assert secret
        assert URL_SAFE.issuperset(secret)

    def test_a_verifier_is_within_the_length_rfc_7636_allows(self) -> None:
        source = self.new_source()

        length = len(source.pkce_verifier())

        assert MIN_SECRET_CHARS <= length <= MAX_PKCE_CHARS

    def test_a_verifier_is_spelt_with_the_unreserved_characters(self) -> None:
        source = self.new_source()

        verifier = source.pkce_verifier()

        assert verifier
        assert UNRESERVED.issuperset(verifier)

    def test_nothing_is_ever_given_out_twice(self) -> None:
        source = self.new_source()

        given = [source.secret() for _ in range(DRAWS)]
        given += [source.pkce_verifier() for _ in range(DRAWS)]

        assert len(set(given)) == len(given)
