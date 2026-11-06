# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The tools of a turn: what binds this build's tools to the turn's owner and agent.

The bound reads are the one place a tool's view of the platform's data is limited to one person
and one agent, so no tool has to, and none can do otherwise.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping

from robinauts.agent_engines.contract.domain import FunctionTool
from robinauts.controller.contract.domain import (
    AgentConfig,
    Message,
    Session,
    SessionNotFoundError,
)
from robinauts.controller.contract.ports import TurnBroker
from robinauts.controller.core.documents import message_from_document
from robinauts.tools.contract.ports import ScopedData, ToolProvider, TurnScope


class OwnedData(ScopedData):
    """The core's reads of one owner's sessions with one agent."""

    __slots__ = ("_agent", "_broker", "_owner")

    def __init__(self, broker: TurnBroker, owner: uuid.UUID, agent: str) -> None:
        self._broker = broker
        self._owner = owner
        self._agent = agent

    async def sessions(self, limit: int, after: Session | None = None) -> list[Session]:
        found: list[Session] = []
        before = None if after is None else (after.updated_at, after.id)
        while len(found) < limit:
            page = await self._broker.sessions_of(self._owner, limit, before)
            found += [s for s in page if s.agent == self._agent][: limit - len(found)]
            if len(page) < limit:
                break
            before = (page[-1].updated_at, page[-1].id)
        return found

    async def messages(self, session_id: uuid.UUID) -> list[Message]:
        session = await self._broker.get_session(self._owner, session_id)
        if session.agent != self._agent:
            raise SessionNotFoundError(str(session_id))
        documents = await self._broker.messages_of(self._owner, session_id)
        return [message_from_document(d) for d in documents]


def functions_for(
    agent: AgentConfig,
    tools: Mapping[str, ToolProvider],
    broker: TurnBroker,
    owner: uuid.UUID,
    session: Session,
) -> tuple[FunctionTool, ...]:
    """The function tools of the turn: those of each of this build's tools the agent names,
    bound to the session's owner and agent."""
    named = [tools[name] for name in agent.tools if name in tools]
    if not named:
        return ()
    scope = TurnScope(session.id, session.agent, OwnedData(broker, owner, session.agent))
    return tuple(function for tool in named for function in tool.tools_for(scope))
