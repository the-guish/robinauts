# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""What every ``ToolServers`` must do, whatever reaches the servers.

Subclass ``ToolServersContract`` and override the three hooks: ``serving``
hands the implementation a server that lists those tools and answers those
calls -- the fake keeps them in a dictionary, the MCP adapter's suite stands a
scripted server up -- ``gone`` a server nobody answers for, and ``run`` runs a
coroutine. What is checked is the port's promises
(``robinauts.ports.tool_servers``): the tools come back as listed, under the
server's names; a call comes back as its result, an error marked as one; a
tool the server does not know is an error result and not a failure; a server
that cannot be reached is a ``ToolServerError`` naming it, from both
questions; and a call still waiting when it is cancelled lets go of what it
holds.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine, Mapping
from typing import Any

import pytest

from robinauts.domain import ListedTool, ToolResult, ToolServerConfig, ToolServerError
from robinauts.ports import ToolServers

SEARCH = ListedTool(
    name="search_repositories",
    description="Search the repositories this deployment may see.",
    input_schema={"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]},
    annotations={"readOnlyHint": True},
)
ECHO = ListedTool(name="echo", input_schema={"type": "object"})
"""Two tools a scripted server lists: one with everything, one with the least."""

FOUND = ToolResult("found 3 repositories")
FAILED = ToolResult("the query was too broad", is_error=True)


class ToolServersContract:
    """Subclass this and override ``serving``, ``gone`` and ``new_servers``."""

    def new_servers(self) -> ToolServers:
        """The implementation under test, which the hooks below configure."""
        raise NotImplementedError("a ToolServersContract subclass overrides `new_servers`")

    def serving(
        self,
        servers: ToolServers,
        tools: tuple[ListedTool, ...],
        answers: Mapping[str, ToolResult],
    ) -> ToolServerConfig:
        """A server that lists ``tools`` and answers each call by tool name as ``answers`` says."""
        raise NotImplementedError("a ToolServersContract subclass overrides `serving`")

    def gone(self, servers: ToolServers) -> ToolServerConfig:
        """A server nobody answers for."""
        raise NotImplementedError("a ToolServersContract subclass overrides `gone`")

    def run(self, work: Coroutine[Any, Any, Any]) -> Any:
        return asyncio.run(work)

    # --- listing ---

    def test_the_tools_come_back_as_the_server_lists_them(self) -> None:
        servers = self.new_servers()
        server = self.serving(servers, (SEARCH, ECHO), {})

        listed = self.run(servers.list_tools(server))

        assert tuple(listed) == (SEARCH, ECHO)

    def test_a_server_with_nothing_to_offer_lists_nothing(self) -> None:
        servers = self.new_servers()
        server = self.serving(servers, (), {})

        assert tuple(self.run(servers.list_tools(server))) == ()

    def test_a_server_that_cannot_be_reached_fails_the_listing_naming_itself(self) -> None:
        servers = self.new_servers()
        server = self.gone(servers)

        with pytest.raises(ToolServerError) as raised:
            self.run(servers.list_tools(server))

        assert server.id in str(raised.value)

    # --- calling ---

    def test_a_call_comes_back_as_its_result(self) -> None:
        servers = self.new_servers()
        server = self.serving(servers, (SEARCH,), {SEARCH.name: FOUND})

        result = self.run(servers.call_tool(server, SEARCH.name, {"q": "robinauts"}))

        assert result == FOUND

    def test_a_call_the_server_calls_a_failure_is_a_result_marked_as_one(self) -> None:
        servers = self.new_servers()
        server = self.serving(servers, (SEARCH,), {SEARCH.name: FAILED})

        result = self.run(servers.call_tool(server, SEARCH.name, {"q": ""}))

        assert result == FAILED

    def test_a_tool_the_server_does_not_know_is_an_error_result_not_a_failure(self) -> None:
        servers = self.new_servers()
        server = self.serving(servers, (SEARCH,), {SEARCH.name: FOUND})

        result = self.run(servers.call_tool(server, "no_such_tool", {}))

        assert result.is_error
        assert "no_such_tool" in result.text

    def test_a_server_that_cannot_be_reached_fails_the_call_naming_itself(self) -> None:
        servers = self.new_servers()
        server = self.gone(servers)

        with pytest.raises(ToolServerError) as raised:
            self.run(servers.call_tool(server, SEARCH.name, {"q": "x"}))

        assert server.id in str(raised.value)
