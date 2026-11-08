# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The pure parts of the side panel's MCP apps: which tool is an app, what a view may call,
the view in a ``resources/read`` result, a ``CallToolResult``, and the answer to a request
among the JSON-RPC messages and the server-sent events a tool server answers with."""

from __future__ import annotations

import base64
from typing import Any

import pytest

from robinauts.web.apps import App, ToolResult, app_of, callable_by_apps, view_text
from robinauts.web.apps import tool_result as result_of
from robinauts.web.mcp_client import EventReader, McpError, answer_in

URI = "ui://board/app.html"
VIEW = "text/html;profile=mcp-app"


def ui(**fields: Any) -> dict[str, Any]:
    return {"_meta": {"ui": fields}}


@pytest.mark.parametrize(
    ("tool", "app"),
    [
        (
            {"name": "board", "title": "Board", "description": "The board.", **ui(resourceUri=URI)},
            App("s", "board", "Board", "The board."),
        ),
        (
            {"name": "board", "annotations": {"title": "Noted"}, **ui(resourceUri=URI)},
            App("s", "board", "Noted", ""),
        ),
        ({"name": "board", "title": "", **ui(resourceUri=URI)}, App("s", "board", "board", "")),
        (
            {"name": "board", **ui(resourceUri=URI, visibility=["app"])},
            App("s", "board", "board", ""),
        ),
        ({"name": "board", **ui(resourceUri=URI, visibility=["model"])}, None),
        ({"name": "board", **ui(resourceUri="https://example.com/app.html")}, None),
        ({"name": "board", **ui(resourceUri="ui://")}, None),
        ({"name": "board", **ui()}, None),
        ({"name": "board", "_meta": {"ui": "ui://board"}}, None),
        ({"name": "board", "_meta": None}, None),
        ({"name": "board"}, None),
        ({"name": "a/b", **ui(resourceUri=URI)}, None),
        ({"name": "a\nb", **ui(resourceUri=URI)}, None),
        ({"name": "", **ui(resourceUri=URI)}, None),
        ({"name": 7, **ui(resourceUri=URI)}, None),
        ({**ui(resourceUri=URI)}, None),
        ({"name": "b", "description": 3, **ui(resourceUri=URI)}, App("s", "b", "b", "")),
    ],
)
def test_an_app_is_a_tool_with_a_view_a_view_may_call(
    tool: dict[str, Any], app: App | None
) -> None:
    assert app_of("s", tool) == app


@pytest.mark.parametrize(
    ("visibility", "callable"),
    [
        (None, True),
        (["model", "app"], True),
        (["app"], True),
        (["model"], False),
        ([], False),
        ("app", False),
    ],
)
def test_a_view_may_call_a_tool_whose_visibility_has_app_or_is_not_set(
    visibility: object, callable: bool
) -> None:
    tool = {"name": "t"} if visibility is None else {"name": "t", **ui(visibility=visibility)}
    assert callable_by_apps(tool) is callable


def blob(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


@pytest.mark.parametrize(
    ("contents", "html"),
    [
        ([{"uri": URI, "mimeType": VIEW, "text": "<p>x</p>"}], "<p>x</p>"),
        ([{"uri": URI, "mimeType": "text/html; profile=mcp-app", "text": "<p>x</p>"}], "<p>x</p>"),
        ([{"uri": URI, "mimeType": VIEW, "blob": blob("<p>é</p>")}], "<p>é</p>"),
        ([{"uri": URI, "mimeType": VIEW, "blob": "not base64!"}], None),
        ([{"uri": URI, "mimeType": VIEW, "blob": base64.b64encode(b"\xff").decode()}], None),
        ([{"uri": URI, "mimeType": "text/html", "text": "<p>x</p>"}], None),
        ([{"uri": "ui://other", "mimeType": VIEW, "text": "<p>x</p>"}], None),
        ([{"uri": URI, "mimeType": VIEW}], None),
        (
            [
                {"uri": "ui://other", "mimeType": VIEW, "text": "a"},
                {"uri": URI, "mimeType": VIEW, "text": "b"},
            ],
            "b",
        ),
        (["text"], None),
        ("text", None),
        (None, None),
    ],
)
def test_the_view_is_the_html_resource_of_its_uri(contents: object, html: str | None) -> None:
    assert view_text({"contents": contents}, URI) == html


@pytest.mark.parametrize(
    ("result", "relayed"),
    [
        ({"content": []}, ToolResult([], None, False)),
        (
            {
                "content": [{"type": "text", "text": "5"}],
                "structuredContent": {"r": 5},
                "isError": True,
            },
            ToolResult([{"type": "text", "text": "5"}], {"r": 5}, True),
        ),
        ({"content": [], "structuredContent": None}, ToolResult([], None, False)),
        ({"content": [], "structuredContent": [1]}, None),
        ({"content": [], "isError": "yes"}, None),
        ({"content": "5"}, None),
        ({}, None),
    ],
)
def test_a_tool_result_is_a_call_tool_result(
    result: dict[str, Any], relayed: ToolResult | None
) -> None:
    assert result_of(result) == relayed


@pytest.mark.parametrize(
    ("message", "result"),
    [
        ({"jsonrpc": "2.0", "id": 1, "result": {"tools": []}}, {"tools": []}),
        ({"jsonrpc": "2.0", "id": 2, "result": {}}, None),
        ({"jsonrpc": "2.0", "method": "notifications/progress"}, None),
        ({"jsonrpc": "2.0", "id": 1}, None),
        ([{"id": 2, "result": {}}, {"id": 1, "result": {"a": 1}}], {"a": 1}),
        ([{"id": 2, "result": {}}], None),
        ("text", None),
    ],
)
def test_the_answer_is_the_result_of_the_request_s_id(
    message: object, result: dict[str, Any] | None
) -> None:
    assert answer_in(message, 1) == result


@pytest.mark.parametrize(
    "message",
    [
        {"jsonrpc": "2.0", "id": 1, "error": {"code": -32601, "message": "secret in here"}},
        {"jsonrpc": "2.0", "id": 1, "result": [1]},
    ],
)
def test_an_error_or_a_result_that_is_not_an_object_is_refused_in_words_of_ours(
    message: object,
) -> None:
    with pytest.raises(McpError) as refused:
        answer_in(message, 1)
    assert "secret" not in str(refused.value)


@pytest.mark.parametrize(
    ("pieces", "events"),
    [
        (["data: a\n\n"], ["a"]),
        (["data: a\r\n\r\n"], ["a"]),
        (["data: a\r\r"], ["a"]),
        (["data: a\r", "\n\r\n"], ["a"]),
        (["data: a\r", "\ndata: b\r\n\r\n"], ["a\nb"]),
        (["da", "ta: a\n", "\n"], ["a"]),
        (["data:a\n\n"], ["a"]),
        (["data:  a\n\n"], [" a"]),
        (["data\n\n"], [""]),
        (["event: message\nid: 7\ndata: a\n\n"], ["a"]),
        ([": keep-alive\n\n", "data: a\n\n"], ["a"]),
        (["\n\n\n"], []),
        (["data: a\ndata: b\n\ndata: c\n\n"], ["a\nb", "c"]),
        (["data: a\n\r"], ["a"]),
        (["data: a\r"], []),
        (["data: cut off"], []),
    ],
)
def test_an_event_is_its_data_once_a_blank_line_ends_it(
    pieces: list[str], events: list[str]
) -> None:
    reader = EventReader()
    fed = [data for piece in pieces for data in reader.feed(piece)]
    assert fed + reader.close() == events
