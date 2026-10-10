# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The Robinauts deployment, as a connector sees it: one endpoint, a turn at a time.

``POST /api/connectors/agents/{agent_id}/turns`` with the connectors' secret as a bearer and
``{"conversation": id | null, "person": {"id", "name"}, "text", "model_id"}``. The answer is the
turn's AG-UI stream over server-sent events, and ``X-Robinauts-Conversation-Id`` names the
conversation, a new one included. The deployment keeps the history: the newest message is all a
turn carries (plan.md, "The API").

The one module that imports ``httpx`` and ``ag_ui``: what leaves it is ``domain``'s, so that no
connector reads an AG-UI event.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass

from robinauts_connectors.domain import AnswerEvent, ConnectorError, Person
from robinauts_connectors.settings import Settings


class ApiError(ConnectorError):
    """The deployment did not take the turn."""


class Unreachable(ApiError):
    """No answer from ``ROBINAUTS_API_URL``."""


class Refused(ApiError):
    """The deployment refused this process or this request: the secret (401), an unknown agent
    or model, a question with no text (404, 422). The message says which, fit for an operator."""


class ConversationGone(ApiError):
    """The conversation named is not one of this person's with this agent (404): deleted, or the
    process was pointed at another agent. The bridge forgets the link and starts a new one."""


class ConversationBusy(ApiError):
    """The conversation has a turn going (409). The bridge runs one turn per thread at a time,
    so this is somebody else writing to the same conversation, such as its owner on the web."""


@dataclass(frozen=True, slots=True)
class Turn:
    conversation: str
    """The id ``X-Robinauts-Conversation-Id`` named."""

    answer: AsyncIterator[AnswerEvent]
    """The turn's AG-UI stream, as ``domain``'s events, ending with ``AnswerFinished`` or
    ``AnswerFailed``."""


class RobinautsApi:
    """One deployment and one agent, as ``Settings`` names them."""

    def __init__(self, settings: Settings) -> None:
        raise NotImplementedError

    async def probe(self) -> str | None:
        """Whether the deployment serves this process, asked once at start-up: ``None``, or a
        sentence saying what is wrong -- no answer, a wrong secret, an unknown agent, an endpoint
        not served. The question has no text, which the endpoint refuses after every other check
        and before it creates anything."""
        raise NotImplementedError

    def turn(
        self, *, conversation: str | None, person: Person, text: str
    ) -> AbstractAsyncContextManager[Turn]:
        """Ask ``text`` in ``conversation``, or in a new one when it is ``None``, for ``person``.
        Entered once the deployment answered with a stream; ``ApiError`` before that. Leaving it
        early closes the stream and not the turn, which ends on the deployment."""
        raise NotImplementedError

    async def aclose(self) -> None:
        raise NotImplementedError
