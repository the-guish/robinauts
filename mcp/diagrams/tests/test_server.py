# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors
"""The server, started as its command and spoken to over streamable HTTP, as a host would."""

import os
import re
import socket
import subprocess
import sys
import time

import httpx
import pytest

from diagrams.server import VIEW_MIME_TYPE, VIEW_URI, view_html

MODEL_TOOLS = {"draw_diagram", "add_to_diagram", "remove_from_diagram", "pin_node", "unpin_node"}


@pytest.fixture(scope="module")
def call():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    process = subprocess.Popen(
        [sys.executable, "-m", "diagrams"],
        env={**os.environ, "DIAGRAMS_PORT": str(port)},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    http = httpx.Client(timeout=10, headers={"Accept": "application/json, text/event-stream"})
    ids = iter(range(1, 1000))

    def request(method, params=None):
        message = {"jsonrpc": "2.0", "id": next(ids), "method": method, "params": params or {}}
        response = http.post(f"http://127.0.0.1:{port}/mcp", json=message)
        assert response.status_code == 200, response.text
        return response.json()["result"]

    deadline = time.monotonic() + 30
    while True:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
            break
        except OSError:
            if process.poll() is not None or time.monotonic() > deadline:
                process.kill()
                pytest.fail("the server did not start")
            time.sleep(0.1)
    try:
        yield request
    finally:
        http.close()
        process.terminate()
        process.wait(timeout=10)


def test_show_diagram_carries_the_view_and_is_the_only_tool_a_view_may_call(call):
    tools = {tool["name"]: tool["_meta"]["ui"] for tool in call("tools/list")["tools"]}
    assert tools.pop("show_diagram") == {"resourceUri": VIEW_URI, "visibility": ["model", "app"]}
    assert set(tools) == MODEL_TOOLS
    assert all(ui == {"visibility": ["model"]} for ui in tools.values())


def test_the_view_is_read_as_an_mcp_app(call):
    contents = call("resources/read", {"uri": VIEW_URI})["contents"]
    assert [(c["mimeType"], c["text"]) for c in contents] == [(VIEW_MIME_TYPE, view_html())]


def test_a_drawing_is_answered_in_words_and_laid_out(call):
    def tool(name, arguments):
        return call("tools/call", {"name": name, "arguments": arguments})

    drawn = tool(
        "draw_diagram",
        {
            "nodes": [{"id": "web", "label": "Web"}, {"id": "db"}],
            "arrows": [{"source": "web", "target": "db"}],
        },
    )
    assert drawn["isError"] is False
    assert drawn["structuredContent"]["order"] == ["web", "db", "web->db"]
    assert re.fullmatch(
        r"Diagram TB, \d+x\d+px:\n- web 'Web' at .*\n- db 'db' at .*\n- web->db",
        drawn["content"][0]["text"],
    )
    pinned = tool("pin_node", {"id": "db", "x": 300, "y": 200})
    assert pinned["structuredContent"]["changed"] == ["db"]
    assert tool("show_diagram", {})["structuredContent"] == pinned["structuredContent"]
    refused = tool("add_to_diagram", {"arrows": [{"source": "web", "target": "nope"}]})
    assert refused["isError"] is True
    assert "names no node 'nope'" in refused["content"][0]["text"]


def test_the_view_is_one_document_that_reaches_nothing_but_its_host():
    html = view_html()
    assert re.findall(r"https?:[^\"]*", html) == ["http://www.w3.org/2000/svg"]  # a name only
    for banned in ["fetch(", "XMLHttpRequest", "WebSocket", "eval(", "innerHTML", " src=", "href="]:
        assert banned not in html
    assert html.count("<script") == html.count("<style") == 1
