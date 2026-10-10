# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The connectors' own vocabulary: who wrote, where, what, and the answer as it arrives.

Nothing here knows a platform or the Robinauts API. A connector turns what its platform delivers
into an ``Incoming``; the bridge turns a turn's AG-UI stream into ``AnswerEvent``s, which the
connector's ``Reply`` shows the way its platform shows a message being written.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Mode(StrEnum):
    """How a connector receives its platform's messages: the two commands of the CLI."""

    CONNECT = "connect"
    """The connector opens the connection: a socket, long polling, a gateway. No public URL, and
    usually one replica per bot."""

    SERVE = "serve"
    """The platform calls the connector: webhooks, on a public HTTPS URL behind the company's
    proxy. Scales out."""


@dataclass(frozen=True, slots=True)
class Person:
    """Somebody writing on a platform, as the platform names them.

    ``id`` is unique across platforms and stable for the person: ``telegram:<user id>``, the same
    in every chat; ``slack:<team>:<member>``. It is the subject the deployment knows them by.
    """

    id: str
    name: str
    handle: str | None = None


@dataclass(frozen=True, slots=True)
class Thread:
    """Where a message was written, in its platform's terms: a private chat, a forum topic, a
    Slack thread or direct message. ``key`` is stable for as long as the platform keeps it."""

    platform: str
    key: str


@dataclass(frozen=True, slots=True)
class Incoming:
    """A message from a person, to the agent, as text.

    ``message_id`` is the platform's, so that a delivery the platform repeats -- Slack retries,
    a webhook answered too late -- is recognised and not asked twice.
    """

    thread: Thread
    author: Person
    text: str
    message_id: str


# --- the answer, as it arrives ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TextDelta:
    text: str


@dataclass(frozen=True, slots=True)
class ToolStarted:
    """The agent calls a tool. Its arguments are attacker-influenced text, and are not passed on:
    a connector shows the tool's name, if anything."""

    call_id: str
    name: str


@dataclass(frozen=True, slots=True)
class ToolEnded:
    call_id: str
    is_error: bool


@dataclass(frozen=True, slots=True)
class AnswerFinished:
    """The answer is whole: everything there was to show has been streamed."""


@dataclass(frozen=True, slots=True)
class AnswerFailed:
    """The turn ended without its answer. ``reason`` is the deployment's fixed sentence, fit to
    show; ``code`` is the turn's state: ``failed``, ``interrupted`` or ``cancelled``."""

    reason: str
    code: str


AnswerEvent = TextDelta | ToolStarted | ToolEnded | AnswerFinished | AnswerFailed
"""What a ``Reply`` is given, in order, ending with ``AnswerFinished`` or ``AnswerFailed``."""


# --- access -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AllowList:
    """Who may use the agent through one platform.

    ``everyone`` lets every person in; otherwise ``entries`` names them by id or by handle, lower
    case and without "@". A platform anybody can find (Telegram) requires one, with ``*`` said
    explicitly; a platform bounded by its workspace (Slack) may leave it open.
    """

    everyone: bool
    entries: frozenset[str] = frozenset()

    @classmethod
    def parse(cls, value: str, *, variable: str, required: bool) -> AllowList:
        """From a comma-separated variable: ids, handles, or ``*``. ``ConfigError`` naming
        ``variable`` when it is required and empty."""
        raise NotImplementedError

    def admits(self, person: Person) -> bool:
        raise NotImplementedError


# --- errors -------------------------------------------------------------------------------


class ConnectorError(Exception):
    """Anything a connector, the bridge or a runner refuses or cannot do."""


class ConfigError(ConnectorError):
    """Settings that cannot be run with. The message names every problem, one per line."""


class UnsupportedModeError(ConnectorError):
    """A connector asked for a mode it does not declare in ``Connector.modes``."""

    def __init__(self, platform: str, mode: Mode) -> None:
        super().__init__(f"{platform} cannot {mode.value}")
        self.platform = platform
        self.mode = mode
