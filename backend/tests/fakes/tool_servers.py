# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Tool servers a test scripts: what each lists, what each tool answers, which are gone."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from typing import Any

from robinauts.domain import ListedTool, ToolResult, ToolServerConfig, ToolServerError
from robinauts.ports import ToolServers

Answer = ToolResult | BaseException | asyncio.Event
"""What a scripted tool does when called: answers, raises, or waits on the event."""


class MemoryToolServers(ToolServers):
    """The port over dictionaries, for tests of everything above it.

    ``listed`` is what each server answers ``tools/list`` with, by server id;
    ``answers`` what each tool answers ``tools/call`` with, by ``(server id,
    tool name)``; a server in ``gone`` cannot be reached. What was asked is
    kept in ``listings`` and ``calls``, in order, so that a test can say what
    the application asked for and how many times.
    """

    def __init__(self) -> None:
        self.listed: dict[str, list[ListedTool]] = {}
        self.answers: dict[tuple[str, str], Answer] = {}
        self.gone: set[str] = set()
        self.listings: list[str] = []
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.open_calls = 0
        """How many calls are waiting on their event: what a cancellation must release."""

    def serving(self, server_id: str, *tools: ListedTool) -> None:
        """Make that server list those tools."""
        self.listed[server_id] = list(tools)

    def answering(self, server_id: str, name: str, answer: Answer) -> None:
        """Make that tool of that server answer so."""
        self.answers[(server_id, name)] = answer

    async def list_tools(self, server: ToolServerConfig) -> Sequence[ListedTool]:
        self.listings.append(server.id)
        if server.id in self.gone:
            raise ToolServerError(f"tool server {server.id!r} cannot be reached")
        return tuple(self.listed.get(server.id, ()))

    async def call_tool(
        self, server: ToolServerConfig, name: str, arguments: Mapping[str, Any]
    ) -> ToolResult:
        self.calls.append((server.id, name, dict(arguments)))
        if server.id in self.gone:
            raise ToolServerError(f"tool server {server.id!r} cannot be reached")
        answer = self.answers.get((server.id, name))
        if answer is None:
            return ToolResult(f"tool server {server.id!r} has no tool {name!r}", is_error=True)
        if isinstance(answer, BaseException):
            raise answer
        if isinstance(answer, asyncio.Event):
            self.open_calls += 1
            try:
                await answer.wait()
            finally:
                self.open_calls -= 1
            return ToolResult("waited")
        return answer
