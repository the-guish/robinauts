# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The MCP apps of the side panel over HTTP (``docs/architecture/web.md``, "Apps"): an agent's
apps are those of the tool servers it names, a view is served under a policy of its own, a
view's tool call is relayed, and what the agent is not given is refused alike. The tool servers
are real MCP servers; the one with the apps wants its secret, and no answer carries it."""

from __future__ import annotations

from collections.abc import Iterator
from typing import TypedDict

import httpx
import pytest

from robinauts.web.apps import NO_SUCH_APP
from robinauts.web.headers import CONTENT_SECURITY_POLICY, VIEW_CONTENT_SECURITY_POLICY
from util import stack
from util.controller_db import requires_postgres
from util.fake_openai import FakeLocalGPTServer
from util.mcp_server import View, mcp_server

pytestmark = requires_postgres

SECRET = "board-secret-4f1d0c9e"
"""The board server's credential, which no answer may carry."""

BOARD_HTML = "<!doctype html><title>Board</title><script>parent.postMessage({}, '*')</script>"


class Item(TypedDict):
    id: int
    text: str
    done: bool


class Board(TypedDict):
    items: list[Item]


def board() -> Board:
    """The board, item by item."""
    return {"items": [{"id": 1, "text": "Buy milk", "done": False}]}


def refresh() -> Board:
    return board()


def peek() -> str:
    """What the model alone may see."""
    return "peeked"


def hidden() -> str:
    return "hidden"


def add(a: int, b: int) -> int:
    """The sum."""
    return a + b


def fail() -> str:
    """Always fails."""
    raise RuntimeError("the board is on fire")


def shout(text: str) -> str:
    return text.upper()


def stolen() -> str:
    return "not yours"


VIEWS = (
    View(board, BOARD_HTML, title="The board"),
    View(refresh, "<p>refresh</p>", visibility=("app",)),
    View(peek, "<p>peek</p>", visibility=("model",)),
    View(hidden, "<p>hidden</p>"),
)

TOOLS = """
[tool_servers.locked]
url = "{board_url}"
secret_env = "NOBODY_SET_THIS"

[tool_servers.gone]
url = "{gone_url}"
auth = "none"
timeout_seconds = 5

[tool_servers.board]
url = "{board_url}"
secret_env = "BOARD_SECRET"
exclude = ["hidden"]

[tool_servers.plain]
url = "{plain_url}"
auth = "none"

[tool_servers.elsewhere]
url = "{elsewhere_url}"
auth = "none"

[agents.with_apps]
title = "With apps"
system_prompt = "{system_prompt}"
model = "local_gpt"
engine = "langchain"
tools = ["locked", "gone", "board", "plain", "sessions"]

[agents.elsewhere]
title = "Elsewhere"
system_prompt = "{system_prompt}"
model = "local_gpt"
engine = "langchain"
tools = ["elsewhere"]
"""


@pytest.fixture(scope="module")
def apps_server(
    model_server: FakeLocalGPTServer, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[str]:
    config = tmp_path_factory.mktemp("apps") / "robinauts.toml"
    with (
        mcp_server(add, fail, views=VIEWS, token=SECRET) as board_url,
        mcp_server(shout) as plain_url,
        mcp_server(views=[View(stolen, "<p>stolen</p>")]) as elsewhere_url,
    ):
        tools = TOOLS.format(
            board_url=board_url,
            gone_url=f"http://127.0.0.1:{stack.free_port()}/mcp",
            plain_url=plain_url,
            elsewhere_url=elsewhere_url,
            system_prompt=stack.SYSTEM_PROMPT,
        )
        config.write_text(stack.config_for(model_server.base_url) + tools)
        env = {**stack.API_KEY, "BOARD_SECRET": SECRET}
        with stack.database(config) as url, stack.server(config, url, env=env) as server:
            yield server


@pytest.fixture
def apps_api(apps_server: str) -> Iterator[httpx.Client]:
    with httpx.Client(base_url=apps_server, timeout=30.0) as client:
        yield client


def test_an_agent_has_the_apps_of_the_servers_it_names_that_answer(apps_api: httpx.Client) -> None:
    listed = apps_api.get("/api/apps", params={"agent": "with_apps"})
    alone = apps_api.get("/api/apps", params={"agent": "elsewhere"})
    without = apps_api.get("/api/apps", params={"agent": "langchain"})
    unknown = apps_api.get("/api/apps", params={"agent": "nobody"})

    assert listed.status_code == 200
    assert listed.json() == {
        "apps": [
            {
                "id": "board/board",
                "server": "board",
                "tool": "board",
                "title": "The board",
                "description": "The board, item by item.",
            },
            {
                "id": "board/refresh",
                "server": "board",
                "tool": "refresh",
                "title": "refresh",
                "description": "",
            },
        ]
    }
    assert [a["id"] for a in alone.json()["apps"]] == ["elsewhere/stolen"]
    assert without.json() == {"apps": []}
    assert unknown.status_code == 404
    assert unknown.json()["error"] == "UnknownAgentError"


def test_a_view_is_served_under_a_policy_of_its_own(apps_api: httpx.Client) -> None:
    view = apps_api.get("/api/apps/board/board/view", params={"agent": "with_apps"})
    page = apps_api.get("/ui/")

    assert view.status_code == 200
    assert view.text == BOARD_HTML
    assert view.headers["content-type"] == "text/html; charset=utf-8"
    assert view.headers.get_list("content-security-policy") == [VIEW_CONTENT_SECURITY_POLICY]
    assert view.headers.get_list("x-frame-options") == ["SAMEORIGIN"]
    assert view.headers["x-content-type-options"] == "nosniff"
    assert view.headers["cache-control"] == "no-store"
    assert page.headers["content-security-policy"] == CONTENT_SECURITY_POLICY
    assert page.headers["x-frame-options"] == "DENY"


def test_a_view_s_tool_call_is_relayed_as_the_server_answered(apps_api: httpx.Client) -> None:
    def call(tool: str, arguments: dict[str, object]) -> httpx.Response:
        body = {"agent": "with_apps", "arguments": arguments}
        return apps_api.post(f"/api/apps/board/tools/{tool}", json=body)

    added = call("add", {"a": 2, "b": 3})
    shown = call("board", {})
    failed = call("fail", {})

    assert added.status_code == 200
    assert added.json() == {
        "content": [{"type": "text", "text": "5"}],
        "structuredContent": {"result": 5},
        "isError": False,
    }
    assert shown.json()["structuredContent"] == board()
    assert failed.status_code == 200
    assert failed.json()["isError"] is True


@pytest.mark.parametrize(
    ("method", "path", "status"),
    [
        ("view", "/api/apps/elsewhere/stolen/view", 404),
        ("view", "/api/apps/nowhere/board/view", 404),
        ("view", "/api/apps/plain/shout/view", 404),
        ("view", "/api/apps/board/add/view", 404),
        ("view", "/api/apps/board/hidden/view", 404),
        ("view", "/api/apps/board/missing/view", 404),
        ("view", "/api/apps/gone/board/view", 404),
        ("view", "/api/apps/locked/board/view", 404),
        ("call", "/api/apps/elsewhere/tools/stolen", 404),
        ("call", "/api/apps/sessions/tools/search_sessions", 404),
        ("call", "/api/apps/board/tools/peek", 404),
        ("call", "/api/apps/board/tools/hidden", 404),
        ("call", "/api/apps/board/tools/missing", 404),
        ("call", "/api/apps/gone/tools/board", 502),
        ("call", "/api/apps/locked/tools/board", 502),
    ],
)
def test_what_the_agent_is_not_given_is_refused_alike(
    apps_api: httpx.Client, method: str, path: str, status: int
) -> None:
    if method == "view":
        answer = apps_api.get(path, params={"agent": "with_apps"})
    else:
        answer = apps_api.post(path, json={"agent": "with_apps", "arguments": {}})

    assert answer.status_code == status
    if status == 404:
        assert answer.json() == {"error": "NotFound", "detail": NO_SUCH_APP}
    else:
        assert answer.json() == {
            "error": "BadGateway",
            "detail": "the tool server did not answer the call",
        }


def test_no_answer_carries_a_tool_server_s_secret(apps_api: httpx.Client) -> None:
    answers = [
        apps_api.get("/api/apps", params={"agent": "with_apps"}),
        apps_api.get("/api/apps/board/board/view", params={"agent": "with_apps"}),
        apps_api.get("/api/apps/board/peek/view", params={"agent": "with_apps"}),
        apps_api.post("/api/apps/board/tools/fail", json={"agent": "with_apps"}),
        apps_api.post("/api/apps/locked/tools/board", json={"agent": "with_apps"}),
        apps_api.get("/api/agents"),
    ]

    for answer in answers:
        assert SECRET not in answer.text, answer.url
        assert SECRET not in str(answer.headers), answer.url
        assert "BOARD_SECRET" not in answer.text, answer.url
