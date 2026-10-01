# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Where a real secret comes from: the operating system's randomness.

The ``SecretSource`` port is implemented once, here, on ``secrets``, which is
the standard library's cryptographically strong source. The module is named
after the port it implements; the import below is the standard library's
``secrets``, which absolute imports make unambiguous.

Nothing else in the platform draws randomness (``docs/layout.md``: ``core`` is
pure, and the application constructs nothing), so a mistake in this file is
the whole of a mistake about how unguessable a session cookie is. It is held
to ``tests/contracts/secrets.py``, the same suite the test double passes.
"""

from __future__ import annotations

from secrets import token_urlsafe

from robinauts.legacy.ports import SECRET_BITS, SecretSource

SECRET_BYTES = SECRET_BITS // 8
"""How many random bytes go into one secret: 32, for the port's 256 bits.

``token_urlsafe`` writes them as base64url without padding, which is 43
characters -- exactly the least ``core.hashing`` accepts, comfortably inside
the 128 RFC 7636 allows a PKCE verifier, and spelt with the URL-safe alphabet
a cookie and a query string carry unescaped.
"""


class OsSecretSource(SecretSource):
    """Secrets from ``secrets.token_urlsafe``: the operating system's randomness.

    Stateless, and therefore safe to share: every call asks the operating
    system again. There is deliberately no seed, no pool and no way to make it
    repeat, because a source that could be made to repeat is a source that
    hands two people the same session.
    """

    def secret(self) -> str:
        """A fresh ``state``, ``nonce`` or session secret of 256 random bits."""
        return token_urlsafe(SECRET_BYTES)

    def pkce_verifier(self) -> str:
        """A fresh PKCE code verifier (RFC 7636 4.1) of 256 random bits.

        The URL-safe alphabet is a subset of the unreserved one RFC 7636 asks
        for, so what ``token_urlsafe`` gives is a valid verifier as it stands.
        """
        return token_urlsafe(SECRET_BYTES)
