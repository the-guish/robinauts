# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The in-memory tool servers, held to the port's contract and to their own script."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping

import pytest

from aio import asyncio_test
from contracts.tool_servers import SEARCH, ToolServersContract
from fakes import MemoryToolServers
from robinauts.domain import ListedTool, ToolResult, ToolServerConfig, ToolServerError
from robinauts.ports import ToolServers


def server(server_id: str) -> ToolServerConfig:
    return ToolServerConfig(id=server_id, url=f"https://{server_id}.example/mcp", secret_env="S")


class TestMemoryToolServers(ToolServersContract):
    def new_servers(self) -> ToolServers:
        return MemoryToolServers()

    def serving(
        self,
        servers: ToolServers,
        tools: tuple[ListedTool, ...],
        answers: Mapping[str, ToolResult],
    ) -> ToolServerConfig:
        assert isinstance(servers, MemoryToolServers)
        servers.serving("scripted", *tools)
        for name, answer in answers.items():
            servers.answering("scripted", name, answer)
        return server("scripted")

    def gone(self, servers: ToolServers) -> ToolServerConfig:
        assert isinstance(servers, MemoryToolServers)
        servers.gone.add("nowhere")
        return server("nowhere")


@asyncio_test
async def test_the_fake_records_what_was_asked_in_order() -> None:
    servers = MemoryToolServers()
    servers.serving("github", SEARCH)
    servers.answering("github", SEARCH.name, ToolResult("found"))

    await servers.list_tools(server("github"))
    await servers.call_tool(server("github"), SEARCH.name, {"q": "a"})
    await servers.call_tool(server("github"), SEARCH.name, {"q": "b"})

    assert servers.listings == ["github"]
    assert servers.calls == [
        ("github", SEARCH.name, {"q": "a"}),
        ("github", SEARCH.name, {"q": "b"}),
    ]


@asyncio_test
async def test_a_scripted_exception_is_raised_from_the_call() -> None:
    servers = MemoryToolServers()
    servers.answering("github", SEARCH.name, ToolServerError("tool server 'github' went away"))

    with pytest.raises(ToolServerError, match="github"):
        await servers.call_tool(server("github"), SEARCH.name, {})


@asyncio_test
async def test_a_call_scripted_to_wait_is_released_when_it_is_cancelled() -> None:
    # What the application's cancellation test needs from underneath: a call
    # that holds until told, and lets go when the task is cancelled.
    servers = MemoryToolServers()
    gate = asyncio.Event()
    servers.answering("github", SEARCH.name, gate)

    task = asyncio.create_task(servers.call_tool(server("github"), SEARCH.name, {}))
    await asyncio.sleep(0)
    assert servers.open_calls == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert servers.open_calls == 0
    gate.set()
    assert await servers.call_tool(server("github"), SEARCH.name, {}) == ToolResult("waited")
