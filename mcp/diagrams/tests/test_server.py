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
    tools = {tool["name"]: tool for tool in call("tools/list")["tools"]}
    shown = tools.pop("show_diagram")
    assert shown["_meta"]["ui"] == {"resourceUri": VIEW_URI, "visibility": ["model", "app"]}
    assert (shown["title"], shown["annotations"]["readOnlyHint"]) == ("Diagram", True)
    assert "to the user in the side panel" in shown["description"]
    assert set(tools) == MODEL_TOOLS
    assert all(tool["_meta"]["ui"] == {"visibility": ["model"]} for tool in tools.values())


def test_every_tool_names_its_diagram_but_a_first_drawing(call):
    tools = call("tools/list")["tools"]
    assert {tool["name"]: tool["inputSchema"]["required"] for tool in tools} == {
        "show_diagram": ["diagram_id"],
        "draw_diagram": ["nodes"],
        "add_to_diagram": ["diagram_id"],
        "remove_from_diagram": ["diagram_id", "ids"],
        "pin_node": ["diagram_id", "id", "x", "y"],
        "unpin_node": ["diagram_id", "id"],
    }
    assert all("diagram_id" in tool["inputSchema"]["properties"] for tool in tools)


def test_the_instructions_say_to_draw_and_then_to_show(call):
    client = {"name": "test", "version": "0"}
    hello = {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": client}
    instructions = call("initialize", hello)["instructions"]
    assert instructions.index("draw_diagram") < instructions.index("show_diagram")
    assert "appear there by themselves" in instructions


def test_the_view_is_listed_as_diagrams_and_read_as_an_mcp_app(call):
    resources = call("resources/list")["resources"]
    assert [(r["uri"], r["title"], r["mimeType"]) for r in resources] == [
        (VIEW_URI, "Diagrams", VIEW_MIME_TYPE)
    ]
    contents = call("resources/read", {"uri": VIEW_URI})["contents"]
    assert [(c["mimeType"], c["text"]) for c in contents] == [(VIEW_MIME_TYPE, view_html())]


def test_a_drawing_is_answered_with_its_id_in_words_and_laid_out(call):
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
    diagram_id = drawn["structuredContent"]["diagram_id"]
    assert re.fullmatch(r"[0-9a-f]{16}", diagram_id)
    assert drawn["structuredContent"]["order"] == ["web", "db", "web->db"]
    assert re.fullmatch(
        rf"Diagram TB, \d+x\d+px \(diagram_id {diagram_id}\):\n"
        r"- web 'Web' at .*\n- db 'db' at .*\n- web->db",
        drawn["content"][0]["text"],
    )
    named = {"diagram_id": diagram_id}
    pinned = tool("pin_node", named | {"id": "db", "x": 300, "y": 200})
    assert pinned["structuredContent"]["changed"] == ["db"]
    assert tool("show_diagram", named)["structuredContent"] == pinned["structuredContent"]
    unpinned = tool("unpin_node", named | {"id": "db"})
    assert [node["pinned"] for node in unpinned["structuredContent"]["nodes"]] == [False, False]
    added = tool("add_to_diagram", named | {"nodes": [{"id": "cache"}]})
    assert added["structuredContent"]["changed"] == ["cache"]
    removed = tool("remove_from_diagram", named | {"ids": ["web"]})
    left = removed["structuredContent"]
    assert [node["id"] for node in left["nodes"]] == ["db", "cache"]
    assert (left["diagram_id"], left["revision"]) == (diagram_id, 5)
    assert f"(diagram_id {diagram_id})" in removed["content"][0]["text"]
    refused = tool("add_to_diagram", named | {"arrows": [{"source": "db", "target": "nope"}]})
    assert refused["isError"] is True
    assert "names no node 'nope'" in refused["content"][0]["text"]


def test_each_drawing_is_a_diagram_of_its_own_until_one_names_another(call):
    def draw(node, **arguments):
        drawn = call(
            "tools/call",
            {"name": "draw_diagram", "arguments": {"nodes": [{"id": node}], **arguments}},
        )
        return drawn["structuredContent"]

    def shown(diagram_id):
        arguments = {"diagram_id": diagram_id}
        content = call("tools/call", {"name": "show_diagram", "arguments": arguments})
        content = content["structuredContent"]
        return content["revision"], [node["id"] for node in content["nodes"]]

    first, second = draw("a")["diagram_id"], draw("b", diagram_id="")["diagram_id"]
    assert first != second
    assert draw("c", diagram_id=first, direction="LR")["diagram_id"] == first
    assert shown(first) == (2, ["c"])
    assert shown(second) == (1, ["b"])


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("show_diagram", {}),
        ("draw_diagram", {"nodes": [{"id": "a"}]}),
        ("add_to_diagram", {"nodes": [{"id": "a"}]}),
        ("remove_from_diagram", {"ids": ["a"]}),
        ("pin_node", {"id": "a", "x": 1, "y": 1}),
        ("unpin_node", {"id": "a"}),
    ],
)
def test_an_id_the_server_does_not_hold_is_a_tool_error_that_says_so(call, name, arguments):
    arguments = {**arguments, "diagram_id": "0123456789abcdef"}
    refused = call("tools/call", {"name": name, "arguments": arguments})
    assert refused["isError"] is True
    assert "there is no diagram '0123456789abcdef'" in refused["content"][0]["text"]


def test_the_view_is_one_document_that_reaches_nothing_but_its_host():
    html = view_html()
    assert re.findall(r"https?:[^\"]*", html) == ["http://www.w3.org/2000/svg"]  # a name only
    for banned in ["fetch(", "XMLHttpRequest", "WebSocket", "eval(", "innerHTML", " src=", "href="]:
        assert banned not in html
    assert html.count("<script") == html.count("<style") == 1


@pytest.mark.parametrize(
    "said",
    [
        'request("ui/initialize", { protocolVersion: "2026-01-26",',
        '"ui/notifications/initialized"',
        '"ui/notifications/tool-input"',
        '"ui/notifications/tool-result"',
        '"ui/notifications/tool-cancelled"',
        '"ui/notifications/host-context-changed"',
        '"ui/resource-teardown"',
        '"ping"',
    ],
)
def test_the_view_speaks_each_message_of_the_protocol(said):
    assert said in view_html()


def test_the_view_reads_its_diagram_again_from_an_animation_frame():
    html = view_html()
    assert '{ name: "show_diagram", arguments: { diagram_id: diagramId } }' in html
    assert "requestAnimationFrame(read)" in html
    assert "const POLL_MS = 2000;" in html
