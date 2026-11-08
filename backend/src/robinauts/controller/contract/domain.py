# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""What crosses the controller port, as plain data (``docs/architecture/controller.md``), beside
the records of ``shared_domain``."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from robinauts.shared_domain.domain import (
    ControllerError,
    Message,
    Session,
    Turn,
    TurnEvent,
)

# --- errors -------------------------------------------------------------------


class InvalidValueError(ControllerError, ValueError):
    pass


class MessageNotFoundError(ControllerError):
    pass


class UnknownAgentError(ControllerError):
    pass


class UnknownModelError(ControllerError):
    pass


class ConfigError(ControllerError):
    """A configuration that cannot be used; the message lists every problem found."""


class TurnActiveError(ControllerError):
    """A turn asked for while the session already has one running."""


class NoActiveTurnError(ControllerError):
    pass


# --- users --------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Identity:
    """Who a shell says is asking: a provider and a subject, as the provider names them, and
    what it said of them that the allow list reads: a verified email, Google's hosted domain,
    the groups."""

    provider: str
    subject: str
    name: str | None = None
    email: str | None = None
    hosted_domain: str | None = None
    groups: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class User:
    """A person; ``name``, ``email``, ``hosted_domain`` and ``groups`` are what their last
    sign-in said, which the allow list is checked against again on every use."""

    id: uuid.UUID
    provider: str
    subject: str
    name: str | None = None
    email: str | None = None
    created_at: datetime | None = None
    hosted_domain: str | None = None
    groups: tuple[str, ...] = ()


# --- credentials --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class UserSession:
    """A signed-in browser. Its cookie holds the secret, and this the secret's SHA-256 hex."""

    id: uuid.UUID
    user_id: uuid.UUID
    secret_hash: str
    created_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class PendingLogin:
    """A sign-in begun and not yet finished, under the SHA-256 hex of its ``state``. Its nonce
    and PKCE verifier are kept as they are, since the callback needs them."""

    state_hash: str
    provider: str
    nonce: str
    verifier: str
    return_to: str
    created_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class ApiToken:
    """Its owner was shown the secret once; this holds the secret's SHA-256 hex."""

    id: uuid.UUID
    user_id: uuid.UUID
    name: str
    secret_hash: str
    created_at: datetime
    expires_at: datetime


LAST_USED_EVERY = timedelta(minutes=1)
"""How often a device's ``last_used_at`` is written: by the resolve that finds it, when the
one recorded is this old or older, so the list says "last used" without a write on every
request."""

PAIRING_WRONG_TRIES = 3
"""The wrong numbers a pairing takes: the last of them deletes it."""


class PairingState(StrEnum):
    WAITING = "waiting"
    CLAIMED = "claimed"
    APPROVED = "approved"


class Approval(StrEnum):
    """What an approve with a number did (``Credentials.approve_pairing``)."""

    APPROVED = "approved"
    WRONG_NUMBER = "wrong_number"
    NOT_CLAIMED = "not_claimed"
    """Waiting for a phone still, or approved already: nothing to decide."""
    GONE = "gone"
    """Unknown, another's, expired, refused, or ended by this, the last wrong number."""


@dataclass(frozen=True, slots=True)
class DeviceDescription:
    """What a phone says of itself when it claims a pairing: data for the list, bounded by the
    shell, and never what anybody decides by."""

    name: str
    platform: str
    app_version: str | None = None


@dataclass(frozen=True, slots=True)
class DeviceSession:
    """A linked device. It holds the secret, and this the secret's SHA-256 hex: the claim hash
    of the pairing that linked it."""

    id: uuid.UUID
    user_id: uuid.UUID
    device: DeviceDescription
    secret_hash: str
    created_at: datetime
    last_used_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class Pairing:
    """A phone being linked (``docs/architecture/device-pairing.md``). Minted ``waiting`` under
    the hash of the code its QR carries; a claim adds the claim secret's hash, the number and
    the device's description; an approve moves the claim hash to the device session."""

    id: uuid.UUID
    user_id: uuid.UUID
    code_hash: str
    state: PairingState
    created_at: datetime
    expires_at: datetime
    claim_hash: str | None = None
    match_number: str | None = None
    wrong_tries: int = 0
    device: DeviceDescription | None = None


# --- catalogue ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AgentListing:
    id: str
    title: str
    default_model: str


@dataclass(frozen=True, slots=True)
class ModelListing:
    id: str
    title: str


# --- sessions -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SessionPage:
    sessions: tuple[Session, ...]
    cursor: str | None = None


@dataclass(frozen=True, slots=True)
class ActiveTurn:
    """The turn a session is in the middle of: which, what it answers, and where its events are."""

    turn_id: uuid.UUID
    follows: uuid.UUID
    position: int


@dataclass(frozen=True, slots=True)
class OpenedSession:
    """One moment of a session: its record, the thread it shows, oldest first, and its turn."""

    session: Session
    messages: tuple[Message, ...]
    active: ActiveTurn | None = None
    ended_badly: Turn | None = None
    """The session's last turn, when it failed, was cancelled or was interrupted."""


# --- turns --------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class NumberedEvent:
    """A turn's event at its position, which is what a watcher asks to continue from."""

    position: int
    event: TurnEvent
