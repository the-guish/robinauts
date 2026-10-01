# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The real secret source, against the contract every source must meet.

The suite is the one in ``tests/contracts/secrets.py``, which the test double
passes too: what a contract is for is that the implementation people rely on
and the one the tests rely on are held to the same promises.
"""

from __future__ import annotations

from contracts.secrets import SecretSourceContract
from robinauts.legacy.adapters import SECRET_BYTES, OsSecretSource
from robinauts.legacy.core import MIN_SECRET_CHARS
from robinauts.legacy.ports import SECRET_BITS, SecretSource


class TestOsSecretSource(SecretSourceContract):
    """The operating system's randomness, held to the whole contract."""

    def new_source(self) -> SecretSource:
        return OsSecretSource()


def test_it_is_a_secret_source() -> None:
    assert isinstance(OsSecretSource(), SecretSource)


def test_it_draws_the_bits_the_port_asks_for() -> None:
    # The port says 256 and nothing may give less; this is where that number
    # turns into a count of bytes.
    assert SECRET_BYTES * 8 == SECRET_BITS


def test_a_secret_is_exactly_the_length_256_bits_are_written_in() -> None:
    # 43 characters of base64url without padding. Longer would mean the
    # source is padding or concatenating; shorter would mean fewer bits.
    source = OsSecretSource()

    assert len(source.secret()) == MIN_SECRET_CHARS
    assert len(source.pkce_verifier()) == MIN_SECRET_CHARS


def test_two_sources_do_not_agree() -> None:
    # Nothing is seeded here: there is deliberately no way to make two of
    # these hand out the same sequence.
    assert OsSecretSource().secret() != OsSecretSource().secret()
