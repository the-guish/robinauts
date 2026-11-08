# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors
"""The server, started as its command and spoken to over streamable HTTP, as a host would."""

import json
import os
import socket
import subprocess
import sys
import time

import httpx
import pytest

from to_do_list.server import VIEW_MIME_TYPE, VIEW_URI, main, view_html

TOOLS = {"show_todos", "add_todo", "toggle_todo", "remove_todo"}


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class Client:
    """Just enough of an MCP client: JSON-RPC over POST, with the session id if one is given."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.http = httpx.Client(timeout=10)
        self.headers = {"Accept": "application/json, text/event-stream"}
        self.next_id = 1

    def post(self, message: dict) -> httpx.Response:
        response = self.http.post(self.url, json=message, headers=self.headers)
        if session := response.headers.get("mcp-session-id"):
            self.headers["mcp-session-id"] = session
        return response

    def request(self, method: str, params: dict | None = None) -> dict:
        self.next_id += 1
        message = {"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params or {}}
        response = self.post(message)
        assert response.status_code == 200, response.text
        assert response.headers["content-type"].startswith("application/json")
        body = response.json()
        assert body["id"] == self.next_id
        return body

    def call(self, name: str, arguments: dict | None = None) -> dict:
        return self.request("tools/call", {"name": name, "arguments": arguments or {}})["result"]


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    port = _free_port()
    data = tmp_path_factory.mktemp("data") / "todos.json"
    environment = {**os.environ, "TO_DO_LIST_PORT": str(port), "TO_DO_LIST_FILE": str(data)}
    process = subprocess.Popen(
        [sys.executable, "-m", "to_do_list"],
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    deadline = time.monotonic() + 30
    while True:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
            break
        except OSError:
            if process.poll() is not None or time.monotonic() > deadline:
                process.kill()
                pytest.fail(f"the server did not start: {process.stderr.read().decode()}")
            time.sleep(0.1)
    client = Client(f"http://127.0.0.1:{port}/mcp")
    client.data = data
    initialized = client.request(
        "initialize",
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "0"},
        },
    )
    assert initialized["result"]["serverInfo"]["name"] == "to-do-list"
    assert client.post({"jsonrpc": "2.0", "method": "notifications/initialized"}).status_code in (
        200,
        202,
    )
    try:
        yield client
    finally:
        client.http.close()
        process.terminate()
        process.wait(timeout=10)
        process.stderr.close()


def test_tools_list_names_the_four_tools_and_the_view_on_show_todos(client):
    tools = {tool["name"]: tool for tool in client.request("tools/list")["result"]["tools"]}
    assert set(tools) == TOOLS
    assert tools["show_todos"]["_meta"]["ui"]["resourceUri"] == VIEW_URI
    for name in TOOLS - {"show_todos"}:
        assert "resourceUri" not in tools[name]["_meta"]["ui"]
    for tool in tools.values():
        assert tool["_meta"]["ui"]["visibility"] == ["model", "app"]
        assert set(tool["outputSchema"]["properties"]) == {"items"}
    assert tools["add_todo"]["inputSchema"]["required"] == ["text"]
    assert tools["toggle_todo"]["inputSchema"]["required"] == ["id"]
    assert tools["remove_todo"]["inputSchema"]["required"] == ["id"]


def test_the_view_is_listed_and_read_as_an_mcp_app(client):
    resources = client.request("resources/list")["result"]["resources"]
    assert [(r["uri"], r["mimeType"]) for r in resources] == [(VIEW_URI, VIEW_MIME_TYPE)]
    contents = client.request("resources/read", {"uri": VIEW_URI})["result"]["contents"]
    assert len(contents) == 1
    assert contents[0]["mimeType"] == VIEW_MIME_TYPE
    assert contents[0]["text"] == view_html()


def test_add_toggle_remove_and_show_answer_with_the_whole_list(client):
    assert client.call("show_todos")["structuredContent"] == {"items": []}
    assert client.call("show_todos")["content"][0]["text"] == "The to-do list is empty."
    client.call("add_todo", {"text": "milk"})
    added = client.call("add_todo", {"text": "bread"})
    assert added["isError"] is False
    assert added["structuredContent"] == {
        "items": [
            {"id": "1", "text": "milk", "done": False},
            {"id": "2", "text": "bread", "done": False},
        ]
    }
    toggled = client.call("toggle_todo", {"id": "1"})
    assert toggled["structuredContent"]["items"][0]["done"] is True
    assert toggled["content"][0]["text"] == (
        "2 to-do(s), 1 done:\n- [x] milk (id 1)\n- [ ] bread (id 2)"
    )
    removed = client.call("remove_todo", {"id": "1"})
    assert removed["structuredContent"] == {"items": [{"id": "2", "text": "bread", "done": False}]}
    assert client.call("show_todos")["structuredContent"] == removed["structuredContent"]


@pytest.mark.parametrize(
    ("name", "arguments", "why"),
    [
        ("toggle_todo", {"id": "404"}, "no to-do with id '404'"),
        ("remove_todo", {"id": "404"}, "no to-do with id '404'"),
        ("add_todo", {"text": "   "}, "needs some text"),
    ],
)
def test_a_refused_change_is_a_tool_error_that_says_why(client, name, arguments, why):
    result = client.call(name, arguments)
    assert result["isError"] is True
    assert why in result["content"][0]["text"]


def test_a_file_that_is_not_a_list_stops_the_start(tmp_path, capsys):
    bad = tmp_path / "todos.json"
    bad.write_text("{", encoding="utf-8")
    assert main(["--file", str(bad)]) == 2
    assert "is not a to-do list" in capsys.readouterr().err


def test_the_list_is_kept_in_the_file_the_variable_names(client):
    shown = client.call("show_todos")["structuredContent"]["items"]
    assert json.loads(client.data.read_text(encoding="utf-8"))["items"] == shown
