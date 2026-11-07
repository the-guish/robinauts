# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The tool port: what every tool shares, and nothing that one tool alone needs.

A tool is given a ``TurnScope`` for each turn. The scope holds no user id and no store: what a
tool may read of the platform's data, it reads through ``ScopedData``, which the worker
binds to the turn's owner and agent before the tool sees it. A tool cannot name another user,
nor another agent's sessions. What a tool may keep on a file system, it keeps in the scope's
``Folder``, bound the same way: the owner's with this agent, and nothing outside it.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass

from robinauts.agent_engines.contract.domain import FunctionTool
from robinauts.filesystems.contract.ports import Folder
from robinauts.shared_domain.domain import Message, Session


class ScopedData(ABC):
    """The platform's own reads, bound to the turn's owner and agent: the store's, without the
    owner, which the worker fills in. Hidden sessions are not found, as in the store."""

    __slots__ = ()

    @abstractmethod
    async def sessions(self, limit: int, after: Session | None = None) -> list[Session]:
        """A page of the agent's sessions of the owner, most recently updated first, past
        ``after``, the last session of the page before."""
        raise NotImplementedError

    @abstractmethod
    async def messages(self, session_id: uuid.UUID) -> list[Message]:
        """Every message of the session, oldest first. ``SessionNotFoundError`` for a session
        that is not one of the owner's with this agent."""
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class TurnScope:
    """The turn a tool's calls are made in."""

    session_id: uuid.UUID
    """The session the turn is in."""
    agent: str
    data: ScopedData
    folder: Folder | None = None
    """The owner's folder with this agent, when the build keeps files; a tool that needs one
    is only ever named beside a build that has one (``cli.composition``)."""


class ToolProvider(ABC):
    """What a tool's ``load()`` returns."""

    __slots__ = ()

    @property
    @abstractmethod
    def id(self) -> str:
        """What an agent's ``tools`` names it by. A tool server may not have the same id."""
        raise NotImplementedError

    @abstractmethod
    def tools_for(self, scope: TurnScope) -> list[FunctionTool]:
        """The tools the model is given for this turn, bound to it."""
        raise NotImplementedError
