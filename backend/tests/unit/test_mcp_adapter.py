# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The MCP adapter against a scripted Streamable HTTP server, with no socket.

The server is an ``httpx.MockTransport`` handler that speaks the protocol the
adapter does -- ``initialize`` with a session id, the ``initialized``
notification, ``tools/list`` in pages, ``tools/call`` -- as JSON or as an
event stream, so that every branch of the client is met by the shape a real
server sends. What it is not: the MCP SDK's reference server, which the
licence gate keeps out (``DEPENDENCIES.md``).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

from aio import asyncio_test
from contracts.tool_servers import ECHO, SEARCH, ToolServersContract
from robinauts.adapters import ToolServerSecrets
from robinauts.adapters.tools.mcp import (
    MAX_PAGES,
    MAX_RESPONSE_BYTES,
    PROTOCOL_VERSION,
    QUIET_CLIENT_LEVEL,
    QUIET_CLIENT_LOGGERS,
    McpToolServers,
    open_client,
)
from robinauts.domain import (
    MAX_PART_CHARS,
    ListedTool,
    ToolResult,
    ToolServerAuth,
    ToolServerConfig,
    ToolServerError,
)
from robinauts.ports import ToolServers

SECRET = "ghp_not-a-real-token"
Reply = Callable[[dict[str, Any]], Any]
"""What a scripted tool does with a call's params: a result mapping, or raises."""


@dataclass
class Script:
    """One scripted server: what it lists, what its tools answer, how it misbehaves."""

    tools: list[dict[str, Any]] = field(default_factory=list)
    answers: dict[str, Any] = field(default_factory=dict)
    """By tool name: a ``tools/call`` result mapping, an ``Exception`` to hang on, or an event."""
    as_stream: bool = False
    """Answer requests as ``text/event-stream`` rather than JSON."""
    session_id: str | None = "session-1"
    page_size: int = 100
    status: int | None = None
    """An HTTP status to answer everything with, for a server that refuses."""
    protocol: str = PROTOCOL_VERSION
    bare: bytes | None = None
    """A body to answer requests with as it is, for a server that speaks something else."""
    content_type: str | None = None
    noise: bool = False
    """Put a notification and an unrelated message in the stream before the answer."""
    seen: list[dict[str, Any]] = field(default_factory=list)
    """Every request body, in order."""
    headers: list[dict[str, str]] = field(default_factory=list)
    """Every request's headers, in order."""
    deleted: int = 0


class ScriptedServers:
    """A transport that routes each request to the script of the host it names."""

    def __init__(self) -> None:
        self.scripts: dict[str, Script] = {}
        self.gone: set[str] = set()

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    async def handle(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host in self.gone:
            raise httpx.ConnectError("connection refused", request=request)
        script = self.scripts[host]
        script.headers.append({key.lower(): value for key, value in request.headers.items()})
        if script.status is not None:
            return httpx.Response(script.status, request=request)
        if request.method == "DELETE":
            script.deleted += 1
            return httpx.Response(200, request=request)
        message = json.loads(request.content)
        script.seen.append(message)
        if "id" not in message:
            return httpx.Response(202, request=request)
        if script.bare is not None:
            return httpx.Response(
                200,
                content=script.bare,
                headers={"content-type": script.content_type or "text/plain"},
            )
        answer = await self.answer(script, message)
        headers = {}
        if message["method"] == "initialize" and script.session_id:
            headers["mcp-session-id"] = script.session_id
        if script.as_stream:
            events = []
            if script.noise:
                events.append({"jsonrpc": "2.0", "method": "notifications/message", "params": {}})
                events.append({"jsonrpc": "2.0", "id": 999, "result": {}})
            events.append(answer)
            body = "".join(f"event: message\ndata: {json.dumps(event)}\n\n" for event in events)
            return httpx.Response(
                200, content=body.encode(), headers={**headers, "content-type": "text/event-stream"}
            )
        return httpx.Response(200, json=answer, headers=headers)

    async def answer(self, script: Script, message: dict[str, Any]) -> dict[str, Any]:
        method, params, request_id = message["method"], message.get("params", {}), message["id"]
        if method == "initialize":
            return _result(
                request_id,
                {
                    "protocolVersion": script.protocol,
                    "capabilities": {},
                    "serverInfo": {"name": "scripted", "version": "0"},
                },
            )
        if method == "tools/list":
            start = int(params.get("cursor", "0") or 0)
            page = script.tools[start : start + script.page_size]
            result: dict[str, Any] = {"tools": page}
            if start + script.page_size < len(script.tools):
                result["nextCursor"] = str(start + script.page_size)
            return _result(request_id, result)
        if method == "tools/call":
            name = params["name"]
            if name not in script.answers:
                return {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32602, "message": f"Unknown tool: {name}"},
                }
            reply = script.answers[name]
            if isinstance(reply, asyncio.Event):
                await reply.wait()
                reply = {"content": [{"type": "text", "text": "waited"}]}
            if callable(reply):
                reply = reply(params)
            return _result(request_id, reply)
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32601, "message": "Method not found"},
        }


def _result(request_id: int, result: Mapping[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": dict(result)}


def as_listed(tool: ListedTool) -> dict[str, Any]:
    """A ``ListedTool`` as a server lists it on the wire."""
    entry: dict[str, Any] = {"name": tool.name, "inputSchema": dict(tool.input_schema)}
    if tool.description:
        entry["description"] = tool.description
    if tool.annotations:
        entry["annotations"] = dict(tool.annotations)
    return entry


def as_answered(result: ToolResult) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": result.text}], "isError": result.is_error}


def server(host: str, **changes: Any) -> ToolServerConfig:
    fields: dict[str, Any] = {
        "id": host.replace(".", "-"),
        "url": f"https://{host}/mcp",
        "secret_env": "S",
    }
    fields.update(changes)
    return ToolServerConfig(**fields)


def adapter(servers: ScriptedServers, **secrets: str) -> McpToolServers:
    return McpToolServers(
        ToolServerSecrets(secrets or {"scripted": SECRET, "nowhere": SECRET}),
        client_for=lambda *, trust_env: httpx.AsyncClient(
            transport=servers.transport()
        ),  # noqa: ARG005
    )


class TestMcpToolServers(ToolServersContract):
    def new_servers(self) -> ToolServers:
        self.scripted = ScriptedServers()
        return adapter(self.scripted)

    def serving(
        self, servers: ToolServers, tools: tuple[ListedTool, ...], answers: Mapping[str, ToolResult]
    ) -> ToolServerConfig:
        self.scripted.scripts["scripted"] = Script(
            tools=[as_listed(tool) for tool in tools],
            answers={name: as_answered(result) for name, result in answers.items()},
        )
        return server("scripted")

    def gone(self, servers: ToolServers) -> ToolServerConfig:
        self.scripted.gone.add("nowhere")
        return server("nowhere")


# --- the session, as the protocol has it ---------------------------------------


def scripted(**changes: Any) -> tuple[ScriptedServers, McpToolServers, Script]:
    servers = ScriptedServers()
    script = Script(tools=[as_listed(SEARCH), as_listed(ECHO)], **changes)
    script.answers.setdefault(SEARCH.name, as_answered(ToolResult("found 3")))
    servers.scripts["scripted"] = script
    return servers, adapter(servers), script


@asyncio_test
async def test_a_question_is_one_session_initialized_asked_and_deleted() -> None:
    servers, tools, script = scripted()

    listed = await tools.list_tools(server("scripted"))

    assert tuple(listed) == (SEARCH, ECHO)
    assert [message["method"] for message in script.seen] == [
        "initialize",
        "notifications/initialized",
        "tools/list",
    ]
    assert script.seen[0]["params"]["protocolVersion"] == PROTOCOL_VERSION
    assert script.seen[0]["params"]["clientInfo"]["name"] == "robinauts"
    # The session id the server handed out goes back on every later request,
    # with the protocol revision agreed, and the session is closed after.
    initialize, initialized, listing, delete = script.headers
    assert "mcp-session-id" not in initialize
    assert initialized["mcp-session-id"] == listing["mcp-session-id"] == "session-1"
    assert listing["mcp-protocol-version"] == PROTOCOL_VERSION
    assert delete["mcp-session-id"] == "session-1" and script.deleted == 1
    assert all(h["accept"] == "application/json, text/event-stream" for h in script.headers[:3])


@asyncio_test
async def test_the_credential_is_a_bearer_header_or_a_basic_one_and_nothing_else() -> None:
    servers, tools, script = scripted()
    servers.scripts["basic"] = Script(tools=[])
    tools = adapter(servers, scripted=SECRET, basic=SECRET)

    await tools.list_tools(server("scripted"))
    await tools.list_tools(
        server("basic", auth=ToolServerAuth.BASIC, user="me@example.com", secret_env="B")
    )

    assert script.headers[0]["authorization"] == f"Bearer {SECRET}"
    pair = base64.b64encode(f"me@example.com:{SECRET}".encode()).decode()
    assert servers.scripts["basic"].headers[0]["authorization"] == f"Basic {pair}"
    assert servers.scripts["basic"].headers[0]["user-agent"].startswith("robinauts")


@asyncio_test
async def test_a_server_without_sessions_is_asked_the_same_way_and_not_deleted() -> None:
    servers, tools, script = scripted(session_id=None)

    await tools.call_tool(server("scripted"), SEARCH.name, {"q": "x"})

    assert all("mcp-session-id" not in h for h in script.headers)
    assert script.deleted == 0


@asyncio_test
async def test_an_answer_as_an_event_stream_is_read_past_the_noise_in_it() -> None:
    servers, tools, script = scripted(as_stream=True, noise=True)

    listed = await tools.list_tools(server("scripted"))
    result = await tools.call_tool(server("scripted"), SEARCH.name, {"q": "x"})

    assert tuple(listed) == (SEARCH, ECHO)
    assert result == ToolResult("found 3")


@asyncio_test
async def test_a_listing_is_followed_page_by_page_and_bounded() -> None:
    many = [
        as_listed(ListedTool(f"tool_{n:03d}", input_schema={"type": "object"})) for n in range(7)
    ]
    servers, tools, script = scripted(page_size=3)
    script.tools = many

    listed = await tools.list_tools(server("scripted"))

    assert [tool.name for tool in listed] == [f"tool_{n:03d}" for n in range(7)]
    assert [
        m.get("params", {}).get("cursor") for m in script.seen if m["method"] == "tools/list"
    ] == [
        None,
        "3",
        "6",
    ]
    script.tools = [as_listed(ECHO)] * (MAX_PAGES + 2)
    script.page_size = 1
    with pytest.raises(ToolServerError, match=f"more than {MAX_PAGES} pages"):
        await tools.list_tools(server("scripted"))


@asyncio_test
async def test_a_listed_tool_this_build_cannot_carry_is_left_out_with_a_line_in_the_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    servers, tools, script = scripted()
    script.tools = [as_listed(SEARCH), {"inputSchema": {}}, "not a tool", {"name": "x" * 300}]

    with caplog.at_level(logging.WARNING, logger="robinauts.adapters.tools.mcp.client"):
        listed = await tools.list_tools(server("scripted"))

    assert tuple(listed) == (SEARCH,)
    assert [r.getMessage() for r in caplog.records] == [
        "tool server 'scripted' listed a tool this build cannot carry (entry 1), left out",
        "tool server 'scripted' listed a tool this build cannot carry (entry 2), left out",
        "tool server 'scripted' listed a tool this build cannot carry (entry 3), left out",
    ]


# --- what a call comes back with -----------------------------------------------


@asyncio_test
async def test_a_result_is_its_text_parts_joined_with_notes_for_the_rest() -> None:
    servers, tools, script = scripted()
    script.answers["mixed"] = {
        "content": [
            {"type": "text", "text": "one"},
            {"type": "image", "data": "AAAA", "mimeType": "image/png"},
            {"type": "resource", "resource": {"uri": "file:///a.txt", "text": "two"}},
            {"type": "resource", "resource": {"uri": "file:///b.bin", "blob": "AAAA"}},
            {"type": "resource_link", "uri": "https://x/y", "name": "y"},
            {"type": "audio", "data": "AAAA", "mimeType": "audio/wav"},
            {"type": "surprise"},
        ]
    }

    result = await tools.call_tool(server("scripted"), "mixed", {})

    assert result == ToolResult(
        "one\n\n[an image (image/png) left out]\n\ntwo\n\n[a resource (file:///b.bin) left out]"
        "\n\n[a link to (https://x/y) left out]\n\n[an audio (audio/wav) left out]"
        "\n\n[content of kind (surprise) left out]"
    )


@asyncio_test
async def test_a_result_with_only_structured_content_is_that_content_as_json() -> None:
    servers, tools, script = scripted()
    script.answers["shaped"] = {"content": [], "structuredContent": {"count": 3, "ok": True}}

    result = await tools.call_tool(server("scripted"), "shaped", {})

    assert result == ToolResult('{"count": 3, "ok": true}')


@asyncio_test
async def test_a_result_the_server_calls_an_error_is_marked_as_one() -> None:
    servers, tools, script = scripted()
    script.answers["failing"] = {
        "content": [{"type": "text", "text": "no such repo"}],
        "isError": True,
    }

    assert await tools.call_tool(server("scripted"), "failing", {}) == ToolResult(
        "no such repo", is_error=True
    )


@asyncio_test
async def test_a_result_longer_than_a_part_is_cut_with_a_note_saying_how_much() -> None:
    servers, tools, script = scripted()
    script.answers["long"] = {"content": [{"type": "text", "text": "x" * (MAX_PART_CHARS + 1000)}]}

    result = await tools.call_tool(server("scripted"), "long", {})

    assert len(result.text) == MAX_PART_CHARS
    assert result.text.endswith(" more characters left out]")
    assert "[1" in result.text[-40:]


@asyncio_test
async def test_the_arguments_travel_as_the_call_s_params() -> None:
    servers, tools, script = scripted()
    script.answers["echo"] = lambda params: {
        "content": [{"type": "text", "text": json.dumps(params)}]
    }

    result = await tools.call_tool(server("scripted"), "echo", {"q": "x", "n": 2})

    assert json.loads(result.text) == {"name": "echo", "arguments": {"q": "x", "n": 2}}


@asyncio_test
async def test_a_call_that_runs_out_of_its_time_is_an_error_result_and_the_session_is_closed() -> (
    None
):
    servers, tools, script = scripted()
    script.answers["slow"] = asyncio.Event()

    result = await tools.call_tool(server("scripted", timeout_seconds=0.05), "slow", {})

    assert result == ToolResult("the call to 'slow' ran out of its 0.05 seconds", is_error=True)
    assert script.deleted == 1


@asyncio_test
async def test_a_call_still_waiting_when_it_is_cancelled_lets_go() -> None:
    servers, tools, script = scripted()
    script.answers["slow"] = asyncio.Event()

    task = asyncio.create_task(tools.call_tool(server("scripted"), "slow", {}))
    await asyncio.sleep(0.01)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


# --- a server that misbehaves ---------------------------------------------------


@asyncio_test
async def test_a_server_that_refuses_the_credential_fails_by_status_and_never_by_secret() -> None:
    for status in (401, 403):
        servers, tools, script = scripted(status=status)

        with pytest.raises(ToolServerError) as raised:
            await tools.list_tools(server("scripted"))

        assert str(raised.value) == (
            f"tool server 'scripted' refused the credential while listing its tools ({status})"
        )
        assert SECRET not in str(raised.value)


@asyncio_test
async def test_a_server_that_answers_another_status_or_not_the_protocol_is_refused() -> None:
    servers, tools, script = scripted(status=500)
    with pytest.raises(ToolServerError, match="answered listing its tools with HTTP 500"):
        await tools.list_tools(server("scripted"))

    servers, tools, script = scripted(bare=b"<html>", content_type="text/html")
    with pytest.raises(ToolServerError, match="not as JSON or an event stream"):
        await tools.list_tools(server("scripted"))

    servers, tools, script = scripted(bare=b"{not json", content_type="application/json")
    with pytest.raises(ToolServerError, match="not JSON"):
        await tools.list_tools(server("scripted"))

    servers, tools, script = scripted(bare=b'{"hello": 1}', content_type="application/json")
    with pytest.raises(ToolServerError, match="not a JSON-RPC message"):
        await tools.list_tools(server("scripted"))

    servers, tools, script = scripted(protocol="1999-01-01")
    with pytest.raises(ToolServerError, match="speaks protocol revision '1999-01-01'"):
        await tools.list_tools(server("scripted"))


@asyncio_test
async def test_an_answer_past_the_bound_is_refused_unread() -> None:
    servers, tools, script = scripted(
        bare=b"x" * (MAX_RESPONSE_BYTES + 1), content_type="application/json"
    )

    with pytest.raises(ToolServerError, match=f"more than {MAX_RESPONSE_BYTES} bytes"):
        await tools.list_tools(server("scripted"))


@asyncio_test
async def test_an_event_stream_that_never_answers_is_refused() -> None:
    servers, tools, script = scripted(
        bare=b'event: message\ndata: {"jsonrpc": "2.0", "method": "notifications/message"}\n\n',
        content_type="text/event-stream",
    )

    with pytest.raises(ToolServerError, match="closed the event stream .* without answering"):
        await tools.list_tools(server("scripted"))


@asyncio_test
async def test_a_server_that_will_not_list_fails_by_name_and_a_call_it_refuses_is_a_result() -> (
    None
):
    servers, tools, script = scripted()
    script.tools = []
    servers.scripts["scripted"].answers = {}

    result = await tools.call_tool(server("scripted"), "nope", {})

    assert result == ToolResult(
        "the server refused the call to 'nope': -32602 Unknown tool: nope", is_error=True
    )


@asyncio_test
async def test_a_server_that_cannot_be_reached_fails_by_name_and_exception_type_only() -> None:
    servers = ScriptedServers()
    servers.gone.add("nowhere")

    with pytest.raises(ToolServerError) as raised:
        await adapter(servers).call_tool(server("nowhere"), "x", {})

    assert str(raised.value) == (
        "tool server 'nowhere' cannot be reached while calling 'x': ConnectError"
    )


# --- pinned like the vendor clients -----------------------------------------------


def test_building_the_adapter_holds_the_http_loggers_at_warning() -> None:
    for name in QUIET_CLIENT_LOGGERS:
        logging.getLogger(name).setLevel(logging.NOTSET)

    McpToolServers(
        ToolServerSecrets({}), client_for=lambda *, trust_env: httpx.AsyncClient()
    )  # noqa: ARG005

    assert all(logging.getLogger(name).level == QUIET_CLIENT_LEVEL for name in QUIET_CLIENT_LOGGERS)


def test_the_client_the_adapter_opens_verifies_follows_no_redirect_and_is_bounded() -> None:
    client = open_client(trust_env=False)

    assert client.follow_redirects is False
    assert client.headers["user-agent"].startswith("robinauts")
    assert client.timeout.connect is not None and client.timeout.read is not None
