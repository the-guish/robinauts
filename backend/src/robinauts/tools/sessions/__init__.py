# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The sessions tool: the model searches the person's earlier sessions with the same agent.

Which sessions those are is not this tool's to decide: it reads through the turn's
``ScopedData``, which the controller bound to the owner and the agent.
"""

from __future__ import annotations

from robinauts.agent_engines.contract.domain import FunctionTool
from robinauts.tools.contract.ports import ToolProvider, TurnScope
from robinauts.tools.sessions.search import search_tool

ID = "sessions"
"""What an agent's ``tools`` names this tool by."""


class SessionsTool(ToolProvider):
    __slots__ = ()

    @property
    def id(self) -> str:
        return ID

    def tools_for(self, scope: TurnScope) -> list[FunctionTool]:
        return [search_tool(scope)]


def load() -> ToolProvider:
    return SessionsTool()
