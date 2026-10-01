# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The counting secret source, against the contract every source must meet."""

from __future__ import annotations

import pytest

from contracts.secrets import SecretSourceContract
from fakes import CountingSecretSource, StuntedSecretSource
from robinauts.legacy.core import MAX_SECRET_CHARS
from robinauts.legacy.ports import SecretSource


class TestCountingSecretSource(SecretSourceContract):
    """Predictable, and the right shape all the same."""

    def new_source(self) -> SecretSource:
        return CountingSecretSource()


class StuntedContract(SecretSourceContract):
    """The same suite over a source that gives too little; it must not pass."""

    def new_source(self) -> SecretSource:
        return StuntedSecretSource()


class OversizedContract(SecretSourceContract):
    """And over one that gives too much, which is as broken and less obvious."""

    def new_source(self) -> SecretSource:
        return StuntedSecretSource(length=MAX_SECRET_CHARS + 1)


@pytest.mark.parametrize(
    "check",
    [
        "test_a_secret_is_long_enough_to_carry_256_bits",
        "test_a_verifier_is_within_the_length_rfc_7636_allows",
        "test_nothing_is_ever_given_out_twice",
    ],
)
def test_the_contract_refuses_a_source_that_gives_too_little(check: str) -> None:
    # The suite is worth what it refuses: a stub that hands out eight
    # characters of nothing in particular fails it.
    with pytest.raises(AssertionError):
        getattr(StuntedContract(), check)()


def test_the_contract_refuses_a_source_that_gives_too_much() -> None:
    # 257 characters is not a secret either: nothing looks one up past 256,
    # so every callback would fail to recognise the session it opened.
    with pytest.raises(AssertionError):
        OversizedContract().test_a_secret_is_short_enough_to_be_looked_up()
