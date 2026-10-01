# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Who signed in: what a provider asserted, and what the platform keeps of it.

An ``Identity`` is a provider's word about a person, taken from one ID token
and never stored as such. A ``User`` is the platform's own record, created at
the first sign-in and keyed by ``(provider, subject)``; a ``Session`` and a
``PendingLogin`` are what a sign-in leaves behind while it lasts.

The secrets these are found by -- the session cookie's value, the ``state`` of
a sign-in in progress -- are not here: the credential store keeps their
SHA-256 alone (``robinauts.legacy.core.hashing``), so a stolen table hands nobody a
session.

Roles are deferred (``docs/working-notes/poc-scope.md``, "Out"): a signed-in
user is the whole of a principal for now.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime

from robinauts.legacy.domain.errors import InvalidValueError

PENDING_LOGIN_MINUTES = 10.0
"""How long a sign-in may take between the button and the callback."""

MAX_PENDING_LOGINS = 10_000
"""How many sign-ins may be in progress at once.

Anyone may begin one without signing in, so this is what keeps a flood of them
from growing the table without end: past it, a sign-in is refused as ``busy``
until some expire.
"""


@dataclass(frozen=True, slots=True)
class Identity:
    """A person, as an identity provider described them at sign-in."""

    provider: str
    """The provider's id in the deployment's configuration."""
    subject: str
    """The provider's stable identifier for the person, never reassigned."""
    name: str | None = None
    email: str | None = None
    email_verified: bool = False
    """Whether the provider checked that the person controls ``email``."""
    hosted_domain: str | None = None
    """Google's ``hd``: the Workspace domain the account belongs to."""
    groups: frozenset[str] = frozenset()
    """The values of the provider's groups claim, if it has one."""

    def __post_init__(self) -> None:
        if not self.provider:
            raise InvalidValueError("an identity names the provider that asserted it")
        if not self.subject:
            raise InvalidValueError("an identity needs the provider's subject")
        # A flag, not something truthy: a ``"no"`` read back out of a store
        # would otherwise be a verified address.
        if not isinstance(self.email_verified, bool):
            raise InvalidValueError(f"email_verified is true or false, not {self.email_verified!r}")
        object.__setattr__(self, "groups", _groups(self.groups))


def _groups(groups: object) -> frozenset[str]:
    """``groups`` as a set of names; ``InvalidValueError`` if it is not one.

    A bare string is refused rather than taken apart: ``"admins"`` as an
    iterable is the letters of the word, and one of them could be a group.
    """
    if isinstance(groups, str) or not isinstance(groups, Iterable):
        raise InvalidValueError(f"groups is a collection of names, not {groups!r}")
    names = frozenset(groups)
    if not all(isinstance(name, str) and name for name in names):
        raise InvalidValueError(f"every group is a non-empty name, not {groups!r}")
    return names


def _aware(when: datetime, what: str) -> datetime:
    """``when`` if it carries a time zone; ``InvalidValueError`` if it does not.

    A naive datetime would be read as local time by one comparison and as UTC
    by the next, and an expiry that means two things is no expiry.
    """
    if not isinstance(when, datetime) or when.tzinfo is None:
        raise InvalidValueError(f"{what} must be an aware datetime, not {when!r}")
    return when


@dataclass(frozen=True, slots=True)
class User:
    """A person with an account here, created at their first sign-in.

    Keyed by ``(provider, subject)``: the same address at two providers is two
    users, and a provider that reassigns an address does not hand over an
    account. The name and the verified email are refreshed at every sign-in;
    they are a display detail, not the identity.
    """

    id: uuid.UUID
    provider: str
    subject: str
    name: str | None = None
    email: str | None = None
    """Their address, only if the provider verified it."""
    created_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.provider or not self.subject:
            raise InvalidValueError("a user is keyed by a provider and a subject")
        if self.created_at is not None:
            _aware(self.created_at, "created_at")

    @property
    def key(self) -> tuple[str, str]:
        """What a user is found by: ``(provider, subject)``."""
        return (self.provider, self.subject)


@dataclass(frozen=True, slots=True)
class Session:
    """A signed-in browser, for a fixed life; the cookie holds its secret."""

    id: uuid.UUID
    user_id: uuid.UUID
    created_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        _aware(self.created_at, "created_at")
        _aware(self.expires_at, "expires_at")

    def has_expired(self, now: datetime) -> bool:
        """Whether this session is over at ``now``; expiry is never extended."""
        return _aware(now, "now") >= self.expires_at


@dataclass(frozen=True, slots=True)
class PendingLogin:
    """A sign-in begun and not finished: what its callback needs.

    Single use and short-lived. It is stored under the SHA-256 of the
    ``state`` handed to the provider, so the row itself proves nothing.
    """

    provider: str
    nonce: str = field(repr=False)
    """Sent to the provider, and expected back in its ID token.

    Out of the repr: it is what proves a callback belongs to this sign-in, so
    a log line, a traceback or a crash report must never carry it.
    """
    verifier: str = field(repr=False)
    """The PKCE code verifier; the provider was sent its challenge.

    Out of the repr for the same reason: with it, an intercepted code can be
    exchanged for a token.
    """
    created_at: datetime
    expires_at: datetime
    return_to: str | None = None
    """Where in the interface the person was, to be sent back to."""

    def __post_init__(self) -> None:
        if not self.provider:
            raise InvalidValueError("a pending sign-in names its provider")
        # An empty nonce would compare equal to a token with no nonce at all,
        # and an empty verifier would hand PKCE away; neither is a sign-in.
        if not self.nonce:
            raise InvalidValueError("a pending sign-in needs its nonce")
        if not self.verifier:
            raise InvalidValueError("a pending sign-in needs its PKCE verifier")
        _aware(self.created_at, "created_at")
        _aware(self.expires_at, "expires_at")

    def has_expired(self, now: datetime) -> bool:
        """Whether this sign-in took too long and must be refused."""
        return _aware(now, "now") >= self.expires_at
