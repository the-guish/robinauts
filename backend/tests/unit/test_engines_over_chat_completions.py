# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Both engines over OpenAI's Chat Completions: one behaviour, held of each.

What the two engines promise alike over the OpenAI kinds, written once and run
under each (the engine is in every test's id). The client half is asserted on
the constructed object; the turn half runs through each engine's **real**
client and real framework over a vendor the test writes
(``tests/chat_completions.py``), because what matters there is what the
framework really sends and really makes of a stream. Nothing reaches a
provider.

What is **not** here is what one engine does and the other does not, or what
only one engine's objects can be asked: those stay in the engine's own module
(``tests/unit/test_langgraph_engine.py``, ``tests/unit/test_pydantic_ai_engine.py``)
-- the client's own fields, what each makes of a reasoning field beside the
text, what each makes of a name repeated on every delta, and each framework's
own tracing. The two engines send different requests for one memory, because
each keeps a memory of its own (ADR 0005): what is held alike is where the
request goes, what signs it, what it carries of the configuration, and what a
turn makes of what comes back.

This module names both engines, and is one of the places the discard test
counts for each (``docs/layout.md``).
"""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import openai
import pytest
import tiktoken
from langchain_core.tools import StructuredTool
from pydantic_ai.toolsets import FunctionToolset

import chat_completions
import robinauts.legacy.adapters.agents.langgraph as langgraph_adapter
import robinauts.legacy.adapters.agents.pydantic_ai as pydantic_ai_adapter
from aio import asyncio_test
from chat_completions import (
    GATEWAY_ENDPOINT,
    GATEWAY_PROVIDER,
    GPT,
    KEY,
    OPENAI_PROVIDER,
    SYSTEM_PROMPT,
)
from contracts.agents import PROMPT, SEARCH
from robinauts.legacy.adapters import ProviderKeys
from robinauts.legacy.adapters.agents.langgraph import LangGraphAgent
from robinauts.legacy.adapters.agents.pydantic_ai import PydanticAIAgent
from robinauts.legacy.core import check_backend_events
from robinauts.legacy.domain import (
    ConfigError,
    Done,
    Engine,
    Event,
    ModelConfig,
    ModelProviderConfig,
    ProviderKind,
    TextDelta,
    ToolCall,
    ToolResult,
)
from robinauts.legacy.ports import Agent

OPENAI_ENDPOINT = "https://api.openai.com/v1"
"""OpenAI's own endpoint, which both engines pin (each adapter's ``OPENAI_ENDPOINT``)."""

COMPATIBLE_ENDPOINT = "https://openrouter.ai/api"
"""An endpoint of the other protocol's, which an unbuilt kind might come with."""

ENGINES = pytest.mark.parametrize("engine", sorted(Engine), ids=lambda engine: engine.value)
"""Every test here, under each engine, with the engine in the test's id."""

KINDS = pytest.mark.parametrize(
    ("provider", "endpoint"),
    [(OPENAI_PROVIDER, OPENAI_ENDPOINT), (GATEWAY_PROVIDER, GATEWAY_ENDPOINT)],
    ids=["openai", "openai-compatible"],
)


def search(q: str) -> str:
    """Search for repositories."""
    return "found 3"


@dataclass(frozen=True)
class Adapter:
    """One engine, and how these tests reach the two seams it has and its OpenAI client.

    The two adapters spell their seams differently and keep the vendor's
    client in different places; this is the one table that knows both, so
    that every test below is written once.
    """

    title: str
    agent: type[Agent]
    module: Any
    model_seam: str
    tools_seam: str
    plain_tools: Callable[..., Any]
    client_of: Callable[[Any], openai.AsyncOpenAI]

    def build(self, models: Any, keys: ProviderKeys, **seams: Any) -> Agent:
        return self.agent(models, keys, **seams)  # type: ignore[call-arg]

    @property
    def chat_model(self) -> Callable[[ModelConfig, ModelProviderConfig, str], Any]:
        return self.module.chat_model  # type: ignore[no-any-return]

    @property
    def endpoint_of(self) -> Callable[[ModelProviderConfig], str]:
        return self.module.endpoint_of  # type: ignore[no-any-return]

    @property
    def client_variables_removed(self) -> tuple[str, ...]:
        return self.module.CLIENT_VARIABLES_REMOVED  # type: ignore[no-any-return]

    @property
    def anthropic_kinds(self) -> frozenset[ProviderKind]:
        return self.module.ANTHROPIC_KINDS  # type: ignore[no-any-return]

    @property
    def openai_kinds(self) -> frozenset[ProviderKind]:
        return self.module.OPENAI_KINDS  # type: ignore[no-any-return]


async def _langchain_tools(*_: object) -> Sequence[Any]:
    return [StructuredTool.from_function(search, name=SEARCH)]


def _pydantic_ai_tools(*_: object) -> Sequence[Any]:
    return [FunctionToolset([search])]


ADAPTERS: dict[Engine, Adapter] = {
    Engine.LANGGRAPH: Adapter(
        "LangGraph",
        LangGraphAgent,
        langgraph_adapter,
        "chat_model_for",
        "tools_for",
        _langchain_tools,
        lambda built: built.root_async_client,
    ),
    Engine.PYDANTIC_AI: Adapter(
        "Pydantic AI",
        PydanticAIAgent,
        pydantic_ai_adapter,
        "model_for",
        "toolsets_for",
        _pydantic_ai_tools,
        lambda built: built.client,
    ),
}


def streamed(*chunks: dict[str, Any]) -> chat_completions.Vendor:
    """A vendor answering every request with those chunks."""
    return chat_completions.Vendor(chat_completions.streamed(*chunks))


def answering(*pieces: str) -> chat_completions.Vendor:
    """A vendor answering with that text, in those pieces, and nothing else."""
    return streamed(*chat_completions.said(*pieces), *chat_completions.finished())


def built(engine: Engine, provider: ModelProviderConfig = OPENAI_PROVIDER, **changes: Any) -> Any:
    """What that engine's factory builds for the OpenAI model."""
    return ADAPTERS[engine].chat_model(chat_completions.gpt(provider, **changes), provider, KEY)


def over_chat_completions(
    engine: Engine,
    vendor: chat_completions.Vendor,
    provider: ModelProviderConfig = OPENAI_PROVIDER,
    **changes: Any,
) -> Agent:
    """That engine, its real OpenAI client answered by that vendor, the tool bound."""
    adapter = ADAPTERS[engine]
    model = built(engine, provider, **changes)
    vendor.plugged_into(adapter.client_of(model))
    return adapter.build(
        chat_completions.openai_models(engine, provider, **changes),
        ProviderKeys({provider.id: KEY}),
        **{adapter.model_seam: lambda *_: model, adapter.tools_seam: adapter.plain_tools},
    )


async def turn_of(agent: Agent, engine: Engine) -> list[Event]:
    """One turn of the OpenAI agent, run to its end."""
    seen: list[Event] = []
    async for event in agent.stream(
        chat_completions.gpt_agent(engine), PROMPT, model=GPT, state=None
    ):
        seen.append(event)
    return seen


def done(events: list[Event]) -> Done:
    (ended,) = [event for event in events if isinstance(event, Done)]
    return ended


def vendor_error(raised: BaseException) -> openai.APIStatusError:
    """The SDK's own exception, from anywhere in that exception's chain.

    LangChain raises it as it is; Pydantic AI wraps it in a ``ModelHTTPError``
    of its own with the SDK's as the cause.
    """
    cause: BaseException | None = raised
    while cause is not None:
        if isinstance(cause, openai.APIStatusError):
            return cause
        cause = cause.__cause__ or cause.__context__
    raise raised


# --- the kinds the engine reaches --------------------------------------------


@ENGINES
def test_every_kind_the_platform_names_is_one_this_engine_offers(engine: Engine) -> None:
    # Two clients, two kinds each; a kind added to the vocabulary tomorrow is
    # a kind the engine does not offer until somebody writes its branch, and
    # this is the line that says so.
    adapter = ADAPTERS[engine]

    assert adapter.agent.kinds == frozenset(ProviderKind)
    assert adapter.anthropic_kinds | adapter.openai_kinds == adapter.agent.kinds
    assert not adapter.anthropic_kinds & adapter.openai_kinds


class _Unbuilt(StrEnum):
    """A kind the platform might name one day, which no engine has a client for."""

    BEDROCK = "bedrock"


@dataclass(frozen=True)
class _UnbuiltProvider:
    """A provider of that kind, built by hand the way no configuration can build one."""

    id: str
    kind: _Unbuilt
    base_url: str | None = None


@ENGINES
@pytest.mark.parametrize("base_url", [None, COMPATIBLE_ENDPOINT])
def test_a_kind_this_engine_does_not_reach_is_refused_rather_than_guessed_at(
    engine: Engine, base_url: str | None
) -> None:
    # The configuration is held to `kinds` at start-up, so nothing should ever
    # get here, and every kind there is today has a branch. If something does
    # -- a kind added to the vocabulary without one, a caller that built a
    # definition by hand -- it must be a refusal naming the provider and never
    # a turn sent to whatever endpoint happened to be nearest, whether or not
    # it came with an address; and the client is refused as the endpoint is,
    # rather than being the Anthropic one by default.
    adapter = ADAPTERS[engine]
    provider: Any = _UnbuiltProvider(id="vendor", kind=_Unbuilt.BEDROCK, base_url=base_url)
    refusal = [
        f"model_providers.vendor: this build of the {adapter.title} engine cannot reach"
        " a bedrock provider"
    ]

    with pytest.raises(ConfigError) as raised:
        adapter.endpoint_of(provider)
    assert list(raised.value.problems) == refusal
    with pytest.raises(ConfigError) as raised:
        adapter.chat_model(ModelConfig(id="m", provider="vendor", name="m"), provider, KEY)
    assert list(raised.value.problems) == refusal


# --- the client the configuration describes ----------------------------------


@ENGINES
@KINDS
def test_an_openai_client_is_the_configuration_s_and_never_the_environment_s(
    monkeypatch: pytest.MonkeyPatch, engine: Engine, provider: ModelProviderConfig, endpoint: str
) -> None:
    for name, value in chat_completions.REDIRECTING_VARIABLES.items():
        monkeypatch.setenv(name, value)
    adapter = ADAPTERS[engine]

    client = adapter.client_of(built(engine, provider))

    assert adapter.endpoint_of(provider) == endpoint
    assert str(client.base_url).rstrip("/") == endpoint
    assert client.api_key == KEY


@ENGINES
def test_the_openai_key_header_is_the_configured_key_whatever_the_environment_injects(
    monkeypatch: pytest.MonkeyPatch, engine: Engine
) -> None:
    # The SDK drops an Authorization line of this variable only when the
    # caller passed one of its own -- and the engine always does.
    monkeypatch.setenv("OPENAI_CUSTOM_HEADERS", "Authorization: Bearer sk-somebody-elses-key")

    sent = chat_completions.headers_of(ADAPTERS[engine].client_of(built(engine)))

    assert sent["authorization"] == f"Bearer {KEY}"


@ENGINES
def test_what_no_argument_can_refuse_reaches_a_client_built_before_the_engine(
    monkeypatch: pytest.MonkeyPatch, engine: Engine
) -> None:
    # The half that shows why the variables have to go: without the engine
    # having been built, the client picks them up, headers and all.
    for name, value in chat_completions.UNCONFIGURED.items():
        monkeypatch.setenv(name, value)

    client = ADAPTERS[engine].client_of(built(engine))
    sent = chat_completions.headers_of(client)

    assert sent["x-nobody-configured"] == "this"
    assert sent["openai-organization"] == "org-somebody-elses"
    assert sent["openai-project"] == "proj-somebody-elses"
    assert client.admin_api_key == "sk-admin-somebody-elses"


@ENGINES
def test_building_the_engine_keeps_every_unconfigured_openai_setting_off_a_turn(
    monkeypatch: pytest.MonkeyPatch, engine: Engine
) -> None:
    for name, value in chat_completions.UNCONFIGURED.items():
        monkeypatch.setenv(name, value)
    adapter = ADAPTERS[engine]

    adapter.build(chat_completions.openai_models(engine), ProviderKeys({"openai": KEY}))
    client = adapter.client_of(built(engine))
    sent = chat_completions.headers_of(client)

    assert all(name not in os.environ for name in chat_completions.UNCONFIGURED)
    assert set(chat_completions.UNCONFIGURED) <= set(adapter.client_variables_removed)
    assert "x-nobody-configured" not in sent
    assert "openai-organization" not in sent
    assert "openai-project" not in sent
    assert sent["authorization"] == f"Bearer {KEY}"
    assert client.admin_api_key is None


# --- a turn --------------------------------------------------------------------


@ENGINES
@asyncio_test
async def test_an_openai_turn_streams_its_text_and_sends_the_configuration_s_request(
    monkeypatch: pytest.MonkeyPatch, engine: Engine
) -> None:
    # Nothing on a turn's path counts tokens with the vendor's tokeniser:
    # tiktoken fetches its encodings from the network the first time it is
    # asked -- or reads them from a warm cache, which closed sockets would not
    # notice -- and it is never asked. The context is measured approximately
    # (docs/specs/agents.md, "A turn").
    monkeypatch.setattr(tiktoken, "get_encoding", chat_completions.never_tokenised)
    monkeypatch.setattr(tiktoken, "encoding_for_model", chat_completions.never_tokenised)
    vendor = answering("Someone ", "who plays ", "fair.")
    agent = over_chat_completions(engine, vendor, timeout_seconds=17.0)

    seen = await turn_of(agent, engine)

    check_backend_events(seen)
    assert [event.text for event in seen if isinstance(event, TextDelta)] == [
        "Someone ",
        "who plays ",
        "fair.",
    ]
    assert done(seen).text == "Someone who plays fair."
    assert isinstance(done(seen).state, bytes)
    assert done(seen).state
    assert agent.held == 0
    sent = vendor.request
    assert str(sent.url) == OPENAI_ENDPOINT + chat_completions.PATH
    assert sent.headers["authorization"] == f"Bearer {KEY}"
    assert sent.headers["x-stainless-read-timeout"] == "17.0"
    body = vendor.body_sent
    assert body["messages"] == [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": PROMPT},
    ]
    assert body["model"] == chat_completions.MODEL_NAME
    assert body["stream"] is True


@ENGINES
@asyncio_test
async def test_an_openai_compatible_turn_goes_to_the_configured_endpoint_and_nowhere_else(
    engine: Engine,
) -> None:
    vendor = answering("Hi.")

    await turn_of(over_chat_completions(engine, vendor, GATEWAY_PROVIDER), engine)

    assert str(vendor.request.url) == GATEWAY_ENDPOINT + chat_completions.PATH
    assert vendor.request.headers["authorization"] == f"Bearer {KEY}"


@ENGINES
@pytest.mark.parametrize(
    ("provider", "field", "not_field"),
    [
        (OPENAI_PROVIDER, "max_completion_tokens", "max_tokens"),
        (GATEWAY_PROVIDER, "max_tokens", "max_completion_tokens"),
    ],
    ids=["openai", "openai-compatible"],
)
@asyncio_test
async def test_a_configured_ceiling_is_sent_in_the_field_the_kind_takes(
    engine: Engine, provider: ModelProviderConfig, field: str, not_field: str
) -> None:
    # Chat Completions requires no ceiling, and on a reasoning model one picked
    # for the answer can be spent on the thinking, so none is sent unless it is
    # configured; one that is goes in the field the kind takes -- OpenAI's
    # current one for OpenAI, the older one OpenRouter and older compatible
    # servers know for a compatible endpoint.
    unbounded = answering("Hi.")
    bounded = chat_completions.Vendor(unbounded.body)

    await turn_of(over_chat_completions(engine, unbounded, provider), engine)
    await turn_of(over_chat_completions(engine, bounded, provider, max_output_tokens=1234), engine)

    assert field not in unbounded.body_sent
    assert not_field not in unbounded.body_sent
    assert bounded.body_sent[field] == 1234
    assert not_field not in bounded.body_sent


@ENGINES
@asyncio_test
async def test_a_tool_round_over_chat_completions_is_run_by_the_framework(
    engine: Engine,
) -> None:
    """The model asks for the tool, the framework runs it and asks again with
    the result, and the turn is done with the answer after it -- two requests,
    the second carrying the call and its result as the protocol spells them."""
    vendor = chat_completions.Vendor(
        bodies=[
            chat_completions.streamed(
                *chat_completions.said("Let me look."),
                *chat_completions.calling("call_1", SEARCH, {"q": "robinauts"}),
                *chat_completions.finished("tool_calls"),
            ),
            chat_completions.streamed(
                *chat_completions.said("Found three."), *chat_completions.finished()
            ),
        ]
    )
    agent = over_chat_completions(engine, vendor)

    seen = await turn_of(agent, engine)

    check_backend_events(seen)
    (called,) = [event for event in seen if isinstance(event, ToolCall)]
    assert (called.call_id, called.name, dict(called.arguments)) == (
        "call_1",
        SEARCH,
        {"q": "robinauts"},
    )
    (answered,) = [event for event in seen if isinstance(event, ToolResult)]
    assert (answered.call_id, answered.name, answered.output, answered.is_error) == (
        "call_1",
        SEARCH,
        "found 3",
        False,
    )
    assert done(seen).text == "Found three."
    assert agent.held == 0
    first, second = (vendor.body_of(request) for request in vendor.sent)
    # The tool, declared as the protocol takes it, on both requests.
    for body in (first, second):
        (tool,) = body["tools"]
        assert (tool["type"], tool["function"]["name"]) == ("function", SEARCH)
    # And the second request carries the round: the call, then its result.
    roles = [message["role"] for message in second["messages"]]
    assert roles == ["system", "user", "assistant", "tool"]
    (call,) = second["messages"][2]["tool_calls"]
    assert (call["id"], call["function"]["name"]) == ("call_1", SEARCH)
    assert second["messages"][3]["tool_call_id"] == "call_1"
    assert "found 3" in str(second["messages"][3]["content"])


@ENGINES
@pytest.mark.parametrize(
    ("status", "error"),
    [
        (401, {"message": "Incorrect API key provided", "type": "invalid_request_error"}),
        (429, {"message": "Rate limit reached", "type": "rate_limit_error"}),
    ],
    ids=["unauthorized", "rate-limited"],
)
@asyncio_test
async def test_what_the_openai_vendor_refuses_travels_out_of_the_turn_as_it_is(
    engine: Engine, status: int, error: dict[str, Any]
) -> None:
    # The port's rule, over this protocol as over the other: an engine reports
    # a failure by raising, and the SDK's own exception is what is raised or
    # what caused it. Nothing is retried on the way (``MAX_RETRIES``): one
    # request, and the turn holds nothing after it.
    vendor = chat_completions.Vendor(status=status, error=error)
    agent = over_chat_completions(engine, vendor)

    with pytest.raises(Exception) as caught:  # noqa: B017 -- each engine's own
        await turn_of(agent, engine)

    said = vendor_error(caught.value)
    assert said.status_code == status
    assert len(vendor.sent) == 1
    assert agent.held == 0
