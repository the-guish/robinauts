# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The kataloop engine builds its client from the settings, keeps the contract, and nothing leaves.

The model is uni-llm-client's real client over ``httpx2.MockTransport``: a scripted vendor
speaking Anthropic's Messages API, streamed, as the client expects it, plugged in through the
engine's seam. The tools of the contract are kataloop's ``LocalToolbox``; the toolbox the engine
reaches MCP servers with is tested against a real one.
"""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import json
import re
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

import httpx
import httpx2
import pytest
from kataloop_core import LocalToolbox, MemoryStore, ModelSpec, ToolOutput, ToolServer, ToolSpec
from mcp.server.fastmcp import Context
from uni_llm_client import Message, ProviderError

from robinauts.agent_engines.contract.domain import (
    MODEL_RETRIES,
    AgentDefinition,
    ModelsConfig,
    ProviderKind,
    ToolServerAuth,
    ToolServerConfig,
    UnknownModelError,
)
from robinauts.agent_engines.contract.ports import (
    AgentEngine,
    EngineSettings,
    ProviderKeyLookup,
    StorageConfig,
    StorageKind,
    ToolSecretLookup,
)
from robinauts.agent_engines.kataloop_engine import engine as engine_module
from robinauts.agent_engines.kataloop_engine import init_kataloop
from robinauts.agent_engines.kataloop_engine.clients import client_for
from robinauts.agent_engines.kataloop_engine.engine import KataloopEngine
from robinauts.agent_engines.kataloop_engine.tools import McpToolbox, tool_server, tools_for
from util.aio import asyncio_test
from util.contracts.engine import (
    ANSWER,
    ARGUMENTS,
    CALL_ID,
    EngineMemoryContract,
    EngineTurnContract,
    ModelFailure,
    Script,
    add,
)
from util.engine_settings import Keys, NoSecrets, settings_for
from util.mcp_server import mcp_server

Handler = Callable[[httpx2.Request], httpx2.Response | Awaitable[httpx2.Response]]


def vendor(handler: Handler) -> httpx2.AsyncClient:
    """An HTTP client whose every request ``handler`` answers, in this process."""
    return httpx2.AsyncClient(transport=httpx2.MockTransport(handler))


# --- Anthropic's Messages API, streamed, as uni-llm-client reads it ---------------------------


def delta(**fields: Any) -> dict[str, Any]:
    return {"type": "content_block_delta", "delta": fields}


def said(*pieces: str) -> list[dict[str, Any]]:
    """A text block, streamed in those pieces."""
    return [
        {"type": "content_block_start", "content_block": {"type": "text", "text": ""}},
        *(delta(type="text_delta", text=piece) for piece in pieces),
    ]


def calling(call_id: str, name: str, arguments: Mapping[str, Any]) -> list[dict[str, Any]]:
    """A tool call: its id and name, then its input as JSON in two pieces."""
    written = json.dumps(arguments)
    middle = len(written) // 2
    return [
        {
            "type": "content_block_start",
            "content_block": {"type": "tool_use", "id": call_id, "name": name, "input": {}},
        },
        *(
            delta(type="input_json_delta", partial_json=piece)
            for piece in (written[:middle], written[middle:])
        ),
    ]


def streamed(*blocks: list[dict[str, Any]], stop_reason: str = "end_turn") -> httpx2.Response:
    """A message of those blocks as the vendor's server-sent events, each block numbered."""
    events: list[dict[str, Any]] = [
        {
            "type": "message_start",
            "message": {
                "id": "msg_robinauts",
                "type": "message",
                "role": "assistant",
                "model": "vendor-name",
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 7, "output_tokens": 1},
            },
        }
    ]
    for index, block in enumerate(blocks):
        events.extend({**event, "index": index} for event in block)
        events.append({"type": "content_block_stop", "index": index})
    events.append(
        {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": {"output_tokens": 5},
        }
    )
    events.append({"type": "message_stop"})
    body = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events)
    return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=body)


def answered(request: httpx2.Request) -> bool:
    """Whether the request's last message carries a tool's result."""
    content = json.loads(request.content)["messages"][-1]["content"]
    return isinstance(content, list) and any(b.get("type") == "tool_result" for b in content)


ANTHROPIC_MESSAGE = {
    "id": "msg_robinauts",
    "type": "message",
    "role": "assistant",
    "model": "vendor-name",
    "content": [{"type": "text", "text": "hi"}],
    "stop_reason": "end_turn",
    "stop_sequence": None,
    "usage": {"input_tokens": 1, "output_tokens": 1},
}

CHAT_COMPLETION = {
    "id": "chatcmpl-robinauts",
    "object": "chat.completion",
    "created": 1,
    "model": "vendor-name",
    "choices": [
        {"index": 0, "message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}
    ],
    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
}


# --- The client, from the settings ------------------------------------------------------------


async def one_call(
    kind: ProviderKind, base_url: str | None, reply: dict[str, Any]
) -> tuple[ModelSpec, httpx2.Request]:
    """The model as kataloop is handed it, and the one request a call on its client sends."""
    sent: list[httpx2.Request] = []

    def answer(request: httpx2.Request) -> httpx2.Response:
        sent.append(request)
        return httpx2.Response(200, json=reply)

    client, spec = client_for("m", settings_for(kind, base_url), http_client=vendor(answer))
    async with client:
        await client.complete(
            spec.id, [Message.user("hi")], max_tokens=spec.max_output_tokens, timeout=spec.timeout
        )
    [request] = sent
    return spec, request


@pytest.mark.parametrize(
    ("kind", "base_url", "endpoint"),
    [
        (ProviderKind.ANTHROPIC, None, "https://api.anthropic.com/v1/messages"),
        (
            ProviderKind.ANTHROPIC_COMPATIBLE,
            "https://gw.example/api",
            "https://gw.example/api/v1/messages",
        ),
    ],
)
@asyncio_test
async def test_an_anthropic_model_is_reached_as_configured(
    kind: ProviderKind, base_url: str | None, endpoint: str
) -> None:
    spec, request = await one_call(kind, base_url, ANTHROPIC_MESSAGE)
    assert spec == ModelSpec("p/vendor-name", timeout=7.0, max_output_tokens=321)
    assert str(request.url) == endpoint
    assert request.headers["x-api-key"] == "key-of-p"
    assert "authorization" not in request.headers
    assert request.extensions["timeout"] == httpx2.Timeout(7.0).as_dict()
    body = json.loads(request.content)
    assert (body["model"], body["max_tokens"]) == ("vendor-name", 321)


@asyncio_test
async def test_an_openai_compatible_model_is_reached_over_chat_completions() -> None:
    spec, request = await one_call(
        ProviderKind.OPENAI_COMPATIBLE, "https://gw.example/v1", CHAT_COMPLETION
    )
    assert spec == ModelSpec("p/vendor-name", timeout=7.0, max_output_tokens=321)
    assert str(request.url) == "https://gw.example/v1/chat/completions"
    assert request.headers["Authorization"] == "Bearer key-of-p"
    assert request.extensions["timeout"] == httpx2.Timeout(7.0).as_dict()
    body = json.loads(request.content)
    assert (body["model"], body["max_tokens"]) == ("vendor-name", 321)


@asyncio_test
async def test_a_model_call_is_retried_model_retries_times() -> None:
    sent: list[httpx2.Request] = []

    def overloaded(request: httpx2.Request) -> httpx2.Response:
        sent.append(request)
        error = {"type": "error", "error": {"type": "overloaded_error", "message": "busy"}}
        # A retry-after of 0 is a retry at once.
        return httpx2.Response(529, headers={"retry-after": "0"}, json=error)

    settings = settings_for(ProviderKind.ANTHROPIC)
    client, spec = client_for("m", settings, http_client=vendor(overloaded))
    async with client:
        with pytest.raises(ProviderError):
            await client.complete(spec.id, [Message.user("hi")])
    assert len(sent) == MODEL_RETRIES + 1


@asyncio_test
async def test_the_window_is_the_configured_one() -> None:
    settings = settings_for(ProviderKind.ANTHROPIC)
    model = dataclasses.replace(settings.models.models["m"], context_window=4000)
    models = dataclasses.replace(settings.models, models={"m": model})
    client, spec = client_for("m", dataclasses.replace(settings, models=models))
    await client.close()
    assert spec.context_window == 4000


def test_a_model_not_in_the_settings_is_refused() -> None:
    with pytest.raises(UnknownModelError):
        client_for("other", settings_for(ProviderKind.ANTHROPIC))


def test_a_model_on_openai_itself_is_refused() -> None:
    with pytest.raises(UnknownModelError, match="openai"):
        client_for("m", settings_for(ProviderKind.OPENAI))


class NoKeys(ProviderKeyLookup):
    def key_for(self, provider_id: str) -> str:
        raise AssertionError(provider_id)


def test_the_engine_answers_the_kinds_uni_llm_client_speaks() -> None:
    engine = KataloopEngine(settings_for(ProviderKind.ANTHROPIC), MemoryStore())
    assert engine.kinds() == frozenset(
        {ProviderKind.ANTHROPIC, ProviderKind.ANTHROPIC_COMPATIBLE, ProviderKind.OPENAI_COMPATIBLE}
    )


@pytest.mark.parametrize(
    "storage",
    [
        StorageConfig(StorageKind.IN_MEMORY, {}),
        StorageConfig(StorageKind.LOCAL, {"path": Path("unused")}),
        StorageConfig(StorageKind.POSTGRES, {"pool": object()}),
    ],
    ids=["in-memory", "local", "postgres"],
)
@asyncio_test
async def test_init_kataloop_keeps_memory_in_this_process_whatever_the_storage(
    storage: StorageConfig,
) -> None:
    settings = EngineSettings(settings_for(ProviderKind.ANTHROPIC).models, NoKeys(), NoSecrets())
    engine = init_kataloop(settings, storage)
    assert isinstance(engine, KataloopEngine)
    assert isinstance(engine._store, MemoryStore)
    # What `robinauts db init` does, with no key in its environment: twice, for nothing.
    await engine.setup()
    await engine.setup()


# --- The tool servers, with their credentials -------------------------------------------------


class FixedSecret(ToolSecretLookup):
    def __init__(self) -> None:
        self.asked: list[str] = []

    def secret_for(self, server_id: str) -> str:
        self.asked.append(server_id)
        return "s3cret"


def tool_settings(secrets: ToolSecretLookup, *servers: ToolServerConfig) -> EngineSettings:
    return EngineSettings(
        models=ModelsConfig(tool_servers={server.id: server for server in servers}),
        keys=Keys(),
        tool_secrets=secrets,
    )


def test_a_bearer_server_is_reached_with_its_secret_as_a_bearer_token() -> None:
    secrets = FixedSecret()
    server = ToolServerConfig(
        id="gh", url="https://mcp.example/gh", timeout_seconds=9.0, exclude=("delete_repo",)
    )
    assert tool_server(server, tool_settings(secrets)) == ToolServer(
        id="gh",
        url="https://mcp.example/gh",
        headers={"Authorization": "Bearer s3cret"},
        timeout=9.0,
        exclude=("delete_repo",),
    )
    assert secrets.asked == ["gh"]


def test_a_basic_server_is_reached_with_its_user_and_secret() -> None:
    secrets = FixedSecret()
    server = ToolServerConfig(
        id="wiki", url="https://mcp.example/wiki", auth=ToolServerAuth.BASIC, user="ana"
    )
    pair = base64.b64encode(b"ana:s3cret").decode("ascii")
    reached = tool_server(server, tool_settings(secrets))
    assert reached.headers == {"Authorization": f"Basic {pair}"}
    assert (reached.url, reached.timeout) == ("https://mcp.example/wiki", 60.0)
    assert secrets.asked == ["wiki"]


def test_a_header_server_is_reached_with_its_secret_in_the_header_it_names() -> None:
    secrets = FixedSecret()
    server = ToolServerConfig(
        id="composio",
        url="https://mcp.example/composio",
        auth=ToolServerAuth.HEADER,
        header="x-api-key",
    )
    assert tool_server(server, tool_settings(secrets)).headers == {"x-api-key": "s3cret"}
    assert secrets.asked == ["composio"]


def test_a_public_server_carries_no_credential_and_asks_for_none() -> None:
    server = ToolServerConfig(id="docs", url="https://mcp.example/docs", auth=ToolServerAuth.NONE)
    assert tool_server(server, tool_settings(NoSecrets())).headers == {}


def test_an_agent_without_tools_has_none() -> None:
    servers, toolbox = tools_for(AgentDefinition("be brief"), tool_settings(NoSecrets()))
    assert servers == ()
    assert isinstance(toolbox, McpToolbox)


def test_an_agent_has_the_servers_it_names_in_its_order() -> None:
    docs = ToolServerConfig(id="docs", url="https://mcp.example/docs", auth=ToolServerAuth.NONE)
    wiki = ToolServerConfig(id="wiki", url="https://mcp.example/wiki", auth=ToolServerAuth.NONE)
    other = ToolServerConfig(id="gh", url="https://mcp.example/gh")
    settings = tool_settings(NoSecrets(), docs, wiki, other)
    servers, _ = tools_for(AgentDefinition("be brief", tools=("wiki", "docs")), settings)
    assert [server.id for server in servers] == ["wiki", "docs"]


# --- The toolbox, against a real MCP server ---------------------------------------------------


def shout(text: str) -> str:
    """The text, in capitals."""
    return text.upper()


def fail(text: str) -> str:
    """Fails, always."""
    raise RuntimeError(f"the tool broke on {text}")


async def nap(seconds: float) -> str:
    """Answers after ``seconds``."""
    await asyncio.sleep(seconds)
    return "rested"


def credential(ctx: Context) -> str:
    """The Authorization header this call came with."""
    request = ctx.request_context.request
    assert request is not None
    return str(request.headers.get("authorization", ""))


@pytest.mark.io
@asyncio_test
async def test_the_toolbox_lists_and_calls_a_server_with_its_credential() -> None:
    toolbox = McpToolbox()
    with mcp_server(shout, credential) as url:
        server = ToolServer("s", url, headers={"Authorization": "Bearer s3cret"}, timeout=10.0)
        listed = await toolbox.tools(server)
        shouted = await toolbox.call(server, "shout", {"text": "hi"})
        sent = await toolbox.call(server, "credential", {})
    assert [(tool.name, tool.description) for tool in listed] == [
        ("shout", "The text, in capitals."),
        ("credential", "The Authorization header this call came with."),
    ]
    assert listed[0].input_schema["properties"]["text"]["type"] == "string"
    assert shouted == ToolOutput("HI")
    assert sent == ToolOutput("Bearer s3cret")


@pytest.mark.io
@asyncio_test
async def test_a_call_is_a_session_of_its_own_and_lists_nothing() -> None:
    """The SDK lists the tools before a result to check its structured content, which the model
    never sees; kataloop's toolbox does not, and neither does this one."""
    sent: list[str] = []

    async def record(request: httpx.Request) -> None:
        if request.method == "POST":
            sent.append(json.loads(request.content)["method"])

    def watched(headers: dict[str, str], timeout: httpx.Timeout) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            headers=headers, timeout=timeout, event_hooks={"request": [record]}
        )

    with mcp_server(shout) as url:
        toolbox = McpToolbox(http_client_factory=watched)
        shouted = await toolbox.call(ToolServer("s", url, timeout=10.0), "shout", {"text": "hi"})
    assert shouted == ToolOutput("HI")
    assert sent == ["initialize", "notifications/initialized", "tools/call"]


@pytest.mark.io
@asyncio_test
async def test_a_tool_that_fails_is_an_error_result_in_its_own_words() -> None:
    with mcp_server(fail) as url:
        output = await McpToolbox().call(ToolServer("s", url, timeout=10.0), "fail", {"text": "x"})
    assert output.is_error
    assert "the tool broke on x" in output.text


@pytest.mark.io
@asyncio_test
async def test_a_tool_past_its_server_s_timeout_is_a_timeout() -> None:
    with mcp_server(nap) as url:
        with pytest.raises(TimeoutError):
            await McpToolbox().call(ToolServer("s", url, timeout=0.5), "nap", {"seconds": 2})


@pytest.mark.io
@asyncio_test
async def test_a_server_out_of_reach_is_its_transport_s_error() -> None:
    with mcp_server(shout) as url:
        pass
    with pytest.raises(Exception) as raised:
        await McpToolbox().tools(ToolServer("s", url, timeout=5.0))
    assert not isinstance(raised.value, BaseExceptionGroup)


# --- The contract ---------------------------------------------------------------------------


def plug(monkeypatch: pytest.MonkeyPatch, handler: Handler) -> None:
    """Every turn's client answered by ``handler``: the real client, from the settings."""
    real = engine_module.client_for

    def plugged(model_id: str, settings: EngineSettings) -> Any:
        return real(model_id, settings, http_client=vendor(handler))

    monkeypatch.setattr(engine_module, "client_for", plugged)


class TestKataloopEngineMemory(EngineMemoryContract):
    @pytest.fixture(autouse=True)
    def scripted_model(self, monkeypatch: pytest.MonkeyPatch) -> None:
        plug(monkeypatch, lambda _: streamed(said("An ", "answer.")))

    async def new_engine(self) -> AgentEngine:
        engine = KataloopEngine(settings_for(ProviderKind.ANTHROPIC), MemoryStore())
        await engine.setup()
        return engine


def scripted(script: Script) -> Handler:
    async def answer(request: httpx2.Request) -> httpx2.Response:
        if script is Script.FAIL:
            raise ModelFailure()
        if script is Script.HANG:
            await asyncio.Event().wait()
        if script is Script.TOOL_ROUND and not answered(request):
            return streamed(calling(CALL_ID, "add", ARGUMENTS), stop_reason="tool_use")
        return streamed(said(*re.split(r"(\s)", ANSWER)))

    return answer


CALCULATOR = ToolServer("calculator", "local")

ADD = ToolSpec(
    "add",
    "Add two integers.",
    {
        "type": "object",
        "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
        "required": ["a", "b"],
    },
)


async def run_add(arguments: Mapping[str, Any]) -> str:
    return str(add(**arguments))


class TestKataloopEngineTurn(EngineTurnContract):
    @pytest.fixture(autouse=True)
    def plain_tool(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def tools_for(*_: object) -> Any:
            return (CALCULATOR,), LocalToolbox({"calculator": [(ADD, run_add)]})

        monkeypatch.setattr(engine_module, "tools_for", tools_for)
        self.monkeypatch = monkeypatch

    async def new_engine(self, script: Script) -> AgentEngine:
        plug(self.monkeypatch, scripted(script))
        engine = KataloopEngine(settings_for(ProviderKind.ANTHROPIC), MemoryStore())
        await engine.setup()
        return engine
