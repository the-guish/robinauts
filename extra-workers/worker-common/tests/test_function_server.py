# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The tools run in the worker's process, served over MCP on loopback: a client that holds a
turn's token lists and calls them, one with another token or none is refused, and a token
unbound serves nothing. Over real HTTP, with httpx as an MCP client speaks it."""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Mapping
from typing import Any

import httpx
import pytest

from robinauts.agent_engines.contract.domain import (
    FunctionTool,
    FunctionToolError,
    ToolServerAuth,
    ToolServerConfig,
)
from robinauts.agent_engines.contract.ports import ToolSecretLookup
from robinauts_worker_common.function_server import FunctionServer
from robinauts_worker_common.network import is_local
from robinauts_worker_common.tool_servers import headers_for


async def _search(arguments: Mapping[str, Any]) -> str:
    if arguments.get("fail") == "meant":
        raise FunctionToolError("nothing matches")
    if arguments.get("fail") == "bug":
        raise KeyError("internal")
    return f"found {arguments.get('q')}"


SEARCH = FunctionTool("search", "Search it", {"type": "object"}, _search)
ACCEPT = {"accept": "application/json, text/event-stream"}


def rpc(method: str, params: dict[str, Any] | None = None, id_: int | None = 1) -> dict:
    message: dict[str, Any] = {"jsonrpc": "2.0", "method": method, "params": params or {}}
    if id_ is not None:
        message["id"] = id_
    return message


def test_a_turns_tools_are_served_under_its_token_alone() -> None:
    async def served() -> None:
        server = FunctionServer()
        await server.start()
        try:
            token = server.bind([SEARCH])
            mine = {**ACCEPT, "authorization": f"Bearer {token}"}
            async with httpx.AsyncClient() as client:

                async def post(message: dict, headers: dict[str, str] = mine) -> httpx.Response:
                    return await client.post(server.url, json=message, headers=headers)

                started = await post(rpc("initialize", {"protocolVersion": "2025-03-26"}))
                assert started.status_code == 200
                assert started.json()["result"]["protocolVersion"] == "2025-03-26"
                assert (await post(rpc("notifications/initialized", id_=None))).status_code == 202
                listed = (await post(rpc("tools/list"))).json()["result"]["tools"]
                assert listed == [
                    {
                        "name": "search",
                        "description": "Search it",
                        "inputSchema": {"type": "object"},
                    }
                ]
                calls = [
                    ({"q": "x"}, "found x", False),
                    ({"fail": "meant"}, "nothing matches", True),
                    ({"fail": "bug"}, "KeyError", True),
                ]
                for arguments, text, failed in calls:
                    params = {"name": "search", "arguments": arguments}
                    result = (await post(rpc("tools/call", params))).json()["result"]
                    assert result == {
                        "content": [{"type": "text", "text": text}],
                        "isError": failed,
                    }
                unknown = (await post(rpc("tools/call", {"name": "nope"}))).json()["result"]
                assert unknown["isError"] is True
                assert (await post(rpc("resources/list"))).json()["error"]["code"] == -32601
                assert (await client.get(server.url, headers=mine)).status_code == 405
                for headers in ({}, {"authorization": "Bearer another"}, ACCEPT):
                    assert (await post(rpc("tools/list"), headers)).status_code == 401
                server.unbind(token)
                assert (await post(rpc("tools/list"))).status_code == 401
        finally:
            await server.stop()

    asyncio.run(served())


class Secrets(ToolSecretLookup):
    def secret_for(self, server_id: str) -> str:
        return "s3cret"


@pytest.mark.parametrize(
    ("table", "headers"),
    [
        (ToolServerConfig("a", "https://a"), {"Authorization": "Bearer s3cret"}),
        (
            ToolServerConfig("a", "https://a", auth=ToolServerAuth.BASIC, user="ada"),
            {"Authorization": "Basic " + base64.b64encode(b"ada:s3cret").decode()},
        ),
        (
            ToolServerConfig("a", "https://a", auth=ToolServerAuth.HEADER, header="X-Key"),
            {"X-Key": "s3cret"},
        ),
        (ToolServerConfig("a", "https://a", auth=ToolServerAuth.NONE), {}),
    ],
)
def test_headers_for(table: ToolServerConfig, headers: dict[str, str]) -> None:
    assert headers_for(table, Secrets()) == headers


@pytest.mark.parametrize(
    ("url", "local"),
    [
        ("http://127.0.0.1:8001/robinauts", True),
        ("http://localhost:8001", True),
        ("http://host.docker.internal:8001", True),
        ("http://10.0.0.5:8001", True),
        ("http://[::1]:8001", True),
        ("unix:///run/robinauts.sock", True),
        ("https://robinauts.example.com", False),
        ("http://8.8.8.8", False),
    ],
)
def test_is_local(url: str, local: bool) -> None:
    assert is_local(url) is local
