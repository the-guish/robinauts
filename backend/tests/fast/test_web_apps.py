# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Web's MCP app routes in process, over a tool server that keeps sessions and answers JSON:
the other way a server may speak from the one ``tests/api/test_apps.py`` meets."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace

import httpx
from echo_controller import CONFIG

from robinauts.cli.composition import compose
from robinauts.shared_domain.domain import (
    StorageConfig,
    StorageKind,
    ToolServerAuth,
    ToolServerConfig,
)
from robinauts.web.headers import VIEW_CONTENT_SECURITY_POLICY
from util.aio import asyncio_test
from util.app import running
from util.mcp_server import View, mcp_server

TOKEN = "fast-token"


def counter(start: int = 0) -> int:
    """One more than ``start``."""
    return start + 1


@asynccontextmanager
async def client(url: str) -> AsyncIterator[httpx.AsyncClient]:
    server = ToolServerConfig(
        "counter", url, secret_env="COUNTER_KEY", auth=ToolServerAuth.HEADER, header="x-api-key"
    )
    echo = replace(CONFIG.agents["echo"], tools=("counter",))
    config = replace(CONFIG, tool_servers={"counter": server}, agents={"echo": echo})
    secrets = {"COUNTER_KEY": TOKEN}.get
    composed = compose(config, storage=StorageConfig(StorageKind.IN_PROCESS), secret_for=secrets)
    async with running(composed, sign_in=None, secret_for=secrets) as app:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as http:
            yield http


@asyncio_test
async def test_an_app_is_listed_viewed_and_called_over_a_server_that_keeps_sessions() -> None:
    view = View(counter, "<p>count</p>", title="Counter")
    with mcp_server(views=[view], sessions=True, json_response=True) as url:
        async with client(url) as http:
            listed = await http.get("/api/apps", params={"agent": "echo"})
            shown = await http.get("/api/apps/counter/counter/view", params={"agent": "echo"})
            called = await http.post(
                "/api/apps/counter/tools/counter",
                json={"agent": "echo", "arguments": {"start": 41}},
            )

    assert [a["id"] for a in listed.json()["apps"]] == ["counter/counter"]
    assert shown.text == "<p>count</p>"
    assert shown.headers["content-security-policy"] == VIEW_CONTENT_SECURITY_POLICY
    assert called.json()["structuredContent"] == {"result": 42}
