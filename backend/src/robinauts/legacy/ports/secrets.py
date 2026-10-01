# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Where a random secret comes from.

``core`` may have no randomness and the application constructs nothing, so the
secrets a sign-in needs -- the ``state``, the ``nonce``, the PKCE verifier and
the session cookie's value -- are asked for here. The one implementation that
matters uses ``secrets``; a test's gives a sequence it chose, so that the hash
a row is stored under is known to the test.

A secret from here is handed out once and never stored: what is kept of it is
its SHA-256 (``robinauts.legacy.core.hashing.secret_hash``).
"""

from __future__ import annotations

from abc import ABC, abstractmethod

SECRET_BITS = 256
"""How much randomness a secret carries; nothing here may give less."""


class SecretSource(ABC):
    """Fresh, unguessable text. Every call returns something new."""

    @abstractmethod
    def secret(self) -> str:
        """A new secret of at least ``SECRET_BITS`` random bits, as URL-safe text.

        Used for the ``state`` of a sign-in, its ``nonce``, and the value of a
        session cookie: each is carried in a URL or a cookie, so it holds
        nothing that would have to be escaped there.
        """
        raise NotImplementedError

    @abstractmethod
    def pkce_verifier(self) -> str:
        """A new PKCE code verifier (RFC 7636 4.1).

        Between 43 and 128 characters of the unreserved set, and at least
        ``SECRET_BITS`` of randomness: the provider is sent only its SHA-256
        challenge, so this is what proves that the code being exchanged is
        being exchanged by whoever asked for it.
        """
        raise NotImplementedError
