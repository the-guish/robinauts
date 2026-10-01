# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Secrets a test knows in advance, so it knows the hash a row is stored under."""

from __future__ import annotations

from robinauts.legacy.ports import SecretSource

LENGTH = 43
"""As long as 256 random bits written as URL-safe text, which is the least the
application accepts. Unguessable these are not, which is the point; the length
and the alphabet are real, so that nothing downstream is exercised against a
shape no real source has.
"""


class CountingSecretSource(SecretSource):
    """``secret-0001sss...``, ``secret-0002sss...``: different every time, and no more.

    A test that has to look a row up needs to know what was handed out.
    ``given`` holds them in order.
    """

    def __init__(self, prefix: str = "secret") -> None:
        self._prefix = prefix
        self._count = 0
        self.given: list[str] = []

    def secret(self) -> str:
        return self._next(self._prefix, "s")

    def pkce_verifier(self) -> str:
        return self._next("verifier", "v")

    def _next(self, prefix: str, padding: str) -> str:
        self._count += 1
        made = f"{prefix}-{self._count:04d}".ljust(LENGTH, padding)
        self.given.append(made)
        return made


class StuntedSecretSource(SecretSource):
    """A source giving the wrong amount: a stub, a truncated read, a runaway loop.

    ``length`` is both ends of the mistake: eight characters anybody could
    guess, or three hundred that no cookie comparison will ever match.
    Nothing may be built on either, and the application is what says so.
    """

    def __init__(self, length: int = 8) -> None:
        self._length = length

    def secret(self) -> str:
        return "x" * self._length

    def pkce_verifier(self) -> str:
        return "x" * self._length
