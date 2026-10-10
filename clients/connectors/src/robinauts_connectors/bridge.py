# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The bridge: the connectors' one ``Inbox``.

For every message a connector submits, in a task of the bridge's own:

1. **Who.** The connector's allow list must admit the author; otherwise the ``Reply`` says so,
   with the id to add, and nothing reaches the deployment.
2. **Which conversation.** The thread link of this person in this thread (``links.py``), or a new
   conversation.
3. **When.** After the turn before it in the same thread has ended: the deployment runs one turn
   per conversation, and a person's second message waits for the first answer instead of being
   refused.
4. **The turn.** Asked of the deployment (``api.py``). A link the deployment no longer answers to
   is forgotten and the question asked again in a new conversation; any other refusal is said to
   the person in one sentence and logged for the operator.
5. **The answer.** Handed to the ``Reply`` as it arrives, and the conversation the deployment named
   kept as the link.

A message the platform delivered twice (the same ``message_id``) is asked once.
"""

from __future__ import annotations

from collections.abc import Sequence
from types import TracebackType
from typing import Self

from robinauts_connectors.api import RobinautsApi
from robinauts_connectors.connector import Connector, Inbox, Reply
from robinauts_connectors.domain import Incoming
from robinauts_connectors.links import ThreadLinks


class Bridge(Inbox):
    """Entered by a runner before any connector starts, and left after they have stopped: leaving
    waits a bounded while for the turns in hand, and abandons the rest, which go on in the
    deployment."""

    def __init__(
        self, api: RobinautsApi, links: ThreadLinks, connectors: Sequence[Connector]
    ) -> None:
        raise NotImplementedError

    def submit(self, message: Incoming, reply: Reply) -> None:
        raise NotImplementedError

    async def handle(self, message: Incoming, reply: Reply) -> None:
        """Steps 1 to 5 above, for one message, to its end."""
        raise NotImplementedError

    async def __aenter__(self) -> Self:
        raise NotImplementedError

    async def __aexit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        trace: TracebackType | None,
    ) -> None:
        raise NotImplementedError
