# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The contract between a platform and the rest of the connectors.

A platform is a ``Connector``: it reads its own settings, says who may use it, and receives its
platform's messages one way or both:

- **connect** (``Connector.connect``): it opens the connection and holds it, handing every
  message to the ``Inbox`` until it is cancelled. The connect runner runs all of them side by side.
- **serve** (``Connector.webhook``): it gives the serve runner a handler for the platform's calls,
  which the runner mounts at ``POST /<name>``. The handler proves the call came from the platform,
  submits the message and answers within the platform's deadline.

Either way a message is submitted with the ``Reply`` that will show its answer, and the connector
does nothing else with it: who may ask, which conversation, when the turn runs and what the
deployment answers are the bridge's (``bridge.py``). A platform's own library is imported in its
sub-package and nowhere else.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import ClassVar, Protocol, Self

from robinauts_connectors.domain import (
    AllowList,
    AnswerEvent,
    Incoming,
    Mode,
    UnsupportedModeError,
)


class Reply(ABC):
    """Where the answer to one incoming message goes. Made by the connector that received the
    message, which alone knows how its platform shows an answer still being written."""

    @abstractmethod
    async def stream(self, answer: AsyncIterator[AnswerEvent]) -> None:
        """Show the answer as it arrives, until its ``AnswerFinished`` or ``AnswerFailed``. The
        platform's limits are the connector's to keep: message length, edit rate, formatting."""
        raise NotImplementedError

    @abstractmethod
    async def say(self, text: str) -> None:
        """One message of the bridge's own, not the agent's, as plain text: a refusal, or that the
        deployment cannot be reached."""
        raise NotImplementedError


class Inbox(Protocol):
    """What a connector hands its messages to: the bridge."""

    def submit(self, message: Incoming, reply: Reply) -> None:
        """Take the message and return at once. Its turn runs in the bridge's own task, so a
        webhook handler can answer within its platform's deadline and a connection's reader is
        never held up by an answer being written."""
        ...


@dataclass(frozen=True, slots=True)
class WebhookRequest:
    """A platform's call, as the serve runner received it: the raw body, which a signature is
    checked over before anything reads it, and lower-cased header names."""

    method: str
    headers: Mapping[str, str]
    query: Mapping[str, str]
    body: bytes


@dataclass(frozen=True, slots=True)
class WebhookResponse:
    status: int = 200
    body: bytes = b""
    headers: Mapping[str, str] = field(default_factory=dict)


Webhook = Callable[[WebhookRequest], Awaitable[WebhookResponse]]
"""A serve-mode connector's handler, free of any web framework: the serve runner alone imports
one."""


class Connector(ABC):
    """One platform, registered under the entry-point group ``robinauts.connectors``: by this
    distribution for its own, by any other distribution for one of its own."""

    name: ClassVar[str]
    """The platform's name: on the command line, as the webhook route (``/<name>``), and as the
    prefix of its people's and threads' ids."""

    modes: ClassVar[frozenset[Mode]]
    """The ways it can receive. A mode it does not declare is refused before anything starts."""

    @classmethod
    @abstractmethod
    def from_environment(cls, environ: Mapping[str, str]) -> Self:
        """Its settings, from its own variables (``TELEGRAM_…``, ``SLACK_…``). ``ConfigError``
        naming every problem at once."""
        raise NotImplementedError

    @property
    @abstractmethod
    def allowed(self) -> AllowList:
        """Who may use the agent through this platform. The bridge asks, so that no connector can
        forget to."""
        raise NotImplementedError

    async def connect(self, inbox: Inbox) -> None:
        """``Mode.CONNECT``: open the connection, submit every message to ``inbox`` with its
        ``Reply``, and hold the connection until cancelled, then close it. Raises when it cannot be
        made, or remade."""
        raise UnsupportedModeError(self.name, Mode.CONNECT)

    def webhook(self, inbox: Inbox) -> Webhook:
        """``Mode.SERVE``: the handler of the platform's calls to ``/<name>``."""
        raise UnsupportedModeError(self.name, Mode.SERVE)
