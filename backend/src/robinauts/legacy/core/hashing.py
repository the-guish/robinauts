# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Secrets: what is kept of one, how two are compared, and what one must be.

SHA-256 where sign-in needs it, the PKCE challenge, and the rules a secret has
to satisfy at either end -- long enough to be one, short enough to look up,
spelt with what a URL and a cookie carry.

Making a secret is not here. It needs randomness, which no pure function has;
the application asks the ``SecretSource`` port for it (``docs/layout.md``,
"core").
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import string

from robinauts.legacy.domain import InvalidValueError

MIN_SECRET_CHARS = 43
"""The shortest a secret of 256 random bits can be written as URL-safe text.

Anything shorter did not come from 256 bits, and a ``state`` or a session
anyone can guess is worse than none. It is also the least RFC 7636 allows a
PKCE verifier to be.
"""

MAX_SECRET_CHARS = 256
"""Longer than any secret this platform hands out.

At the reading end, what is longer is not looked up: a cookie or a query
parameter is whatever anyone put there, and hashing a megabyte of it would be
work done on request. At the making end, a source that gives more than this is
broken in a way that would make every callback fail its ``state`` comparison.
"""

MAX_PKCE_CHARS = 128
"""The longest a PKCE code verifier may be (RFC 7636 4.1)."""

URL_SAFE = frozenset(string.ascii_letters + string.digits + "-_")
"""What ``secrets.token_urlsafe`` gives: what a cookie and a query carry as is."""

UNRESERVED = frozenset(string.ascii_letters + string.digits + "-._~")
"""RFC 7636 4.1's alphabet for a code verifier: RFC 3986's unreserved set."""


def secret_hash(secret: str) -> str:
    """What the credential store keeps of a secret, and finds it by.

    Session cookies and the ``state`` of a sign-in in progress each carry 256
    random bits and are never chosen by a person, so a plain hash is enough to
    make a stolen table useless; and a lookup by hash needs no comparison in
    constant time, since the hash of a guess is not the hash of the secret.
    """
    return hashlib.sha256(secret.encode("utf-8", "surrogatepass")).hexdigest()


def pkce_challenge(verifier: str) -> str:
    """The ``S256`` code challenge for a PKCE verifier (RFC 7636 4.2).

    Base64url of the SHA-256 of the verifier's ASCII, without padding.
    """
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def is_secret_shaped(secret: object) -> bool:
    """Whether ``secret`` could be one of ours, and is worth hashing at all."""
    return isinstance(secret, str) and 0 < len(secret) <= MAX_SECRET_CHARS


def same_secret(one: str, other: str) -> bool:
    """Whether two secrets are the same, compared in constant time.

    ``False`` for anything that is not shaped like a secret, before any
    comparison: an empty cookie must not equal an empty query parameter.

    As bytes: ``compare_digest`` refuses a ``str`` holding anything but ASCII,
    and both of these are whatever a browser sent. Encoded with
    ``surrogatepass``, because a lone surrogate must be a mismatch, not a
    ``UnicodeEncodeError`` escaping a sign-in.
    """
    if not is_secret_shaped(one) or not is_secret_shaped(other):
        return False
    return hmac.compare_digest(
        one.encode("utf-8", "surrogatepass"), other.encode("utf-8", "surrogatepass")
    )


def checked_secret(
    secret: object,
    what: str,
    *,
    most: int = MAX_SECRET_CHARS,
    alphabet: frozenset[str] = URL_SAFE,
) -> str:
    """``secret`` if a source gave something worth being one; raise if not.

    ``InvalidValueError``, which is a mistake in the deployment rather than a
    sign-in that failed: a stub left in a build, a counter, a truncated read of
    ``/dev/urandom``, something that is not text at all. The alternatives are
    worse -- a short one is guessable, a long one passes every ``state``
    comparison at the callback into ``state_mismatch``, and one with a
    character a URL escapes arrives at the provider as something else.

    Both ends are checked, and the alphabet with them, because this is the one
    place between a source and a person's session.
    """
    if not isinstance(secret, str):
        raise InvalidValueError(
            f"the secret source gave a {what} that is not text: {type(secret).__name__}"
        )
    if not MIN_SECRET_CHARS <= len(secret) <= most:
        raise InvalidValueError(
            f"the secret source gave a {what} of {len(secret)} characters; "
            f"a {what} is {MIN_SECRET_CHARS} to {most}"
        )
    if not alphabet.issuperset(secret):
        raise InvalidValueError(
            f"the secret source gave a {what} spelt with characters a URL does not "
            f"carry as they are"
        )
    return secret
