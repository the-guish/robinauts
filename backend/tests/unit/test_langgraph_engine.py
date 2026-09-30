# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The LangChain engine: the contract suite, the mapping, and what it never does.

The engine is run for real -- its agent built by ``create_agent``, its loop
run by the framework, its stream consumed, its ``finally`` exercised -- over a
chat model the test scripts and tools that are plain functions. No network,
no key: what is replaced is the two seams the adapter has for them
(``LangGraphAgent(chat_model_for=..., tools_for=...)``), and everything else
is the code a deployment runs.

Five subjects:

- the **contract** both engines are held to (``contracts/agents.py``), run
  here over the real engine;
- the **mapping**: what the model is given -- the system prompt, the memory,
  the tools -- and what the framework's stream becomes: deltas, calls,
  results, and the memory handed back;
- the **loop and the context**, which are the framework's: a tool that fails
  is a result the model reads, a turn that never stops is bounded, and a
  history past the window is summarised by the middleware without its words
  reaching the answer;
- the **client**: that a model's configuration reaches Anthropic's client as
  the key, the timeout, the retries and the ceiling it describes, and a tool
  server's reaches the framework's MCP client as the endpoint, the credential
  and the timeout, asserted on the constructed object because constructing
  one reaches nothing;
- **nothing phones home**: with the environment asking loudly for tracing, a
  turn attaches no tracer and this process has never built a LangSmith client.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import subprocess
import sys
from collections.abc import AsyncIterator, Iterator, Sequence
from typing import Any

import anthropic
import langsmith
import langsmith._internal._context
import langsmith.run_trees
import pytest
from langchain.agents.middleware import SummarizationMiddleware
from langchain_anthropic import ChatAnthropic
from langchain_anthropic.middleware import AnthropicPromptCachingMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, HumanMessage
from langchain_core.messages.tool import tool_call_chunk
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.tools import BaseTool, StructuredTool, ToolException
from langchain_core.tracers import langchain as tracer_module
from langchain_core.tracers.context import _tracing_v2_is_enabled
from langgraph.errors import GraphRecursionError

from aio import asyncio_test
from conftest import VENDOR_LOGGERS
from contracts.agents import PROMPT, SEARCH, AgentContract, Ending, Script
from conversations import agent_definition
from robinauts.adapters import ProviderKeys, ToolServerSecrets
from robinauts.adapters.agents.langgraph import (
    ANTHROPIC_ENDPOINT,
    ANTHROPIC_KEY_HEADER,
    CLIENT_VARIABLES_REMOVED,
    DEFAULT_ANTHROPIC_OUTPUT_TOKENS,
    DEFAULT_CONTEXT_WINDOW,
    MAX_RETRIES,
    MAX_TOOL_ROUNDS,
    QUIET_CLIENT_LEVEL,
    QUIET_CLIENT_LOGGERS,
    TRACING_VARIABLES_REMOVED,
    LangGraphAgent,
    chat_model,
    context_window,
    endpoint_of,
    mcp_connection,
    middleware,
    read_state,
    write_state,
)
from robinauts.core import check_backend_events
from robinauts.domain import (
    KINDS_WITH_BASE_URL,
    MAX_PART_CHARS,
    AgentDefinition,
    ConfigError,
    Done,
    Engine,
    Event,
    InvalidValueError,
    ModelConfig,
    ModelProviderConfig,
    ModelsConfig,
    ProviderKind,
    ReasoningDelta,
    TextDelta,
    ToolCall,
    ToolResult,
    ToolServerAuth,
    ToolServerConfig,
    UnknownModelError,
)
from robinauts.ports import Agent

PROVIDER = "anthropic"
MODEL = "sonnet"
AGENT = "assistant"
KEY = "not-a-real-key"
"""What the tests hand the engine. Nothing here reaches a provider."""

SYSTEM_PROMPT = "Play fair."

PIECES = 3
"""How many deltas a streamed answer is scripted to arrive in."""

ANTHROPIC_PROVIDER = ModelProviderConfig(
    id=PROVIDER, kind=ProviderKind.ANTHROPIC, api_key_env="ROBINAUTS_ANTHROPIC_KEY"
)

COMPATIBLE_ENDPOINT = "https://openrouter.ai/api"
"""An endpoint that speaks Anthropic's Messages API, at the address of one.

OpenRouter's, spelt as an operator writes it: a **prefix** the client appends
``/v1/messages`` to, which is why it stops at ``/api``
(``docs/specs/agents.md``).
"""

COMPATIBLE_PROVIDER = ModelProviderConfig(
    id="openrouter",
    kind=ProviderKind.ANTHROPIC_COMPATIBLE,
    api_key_env="ROBINAUTS_OPENROUTER_KEY",
    base_url=COMPATIBLE_ENDPOINT,
)

GITHUB = ToolServerConfig(
    id="github", url="https://mcp.example.test/github", secret_env="GITHUB_TOKEN"
)
DOCS = ToolServerConfig(
    id="docs", url="https://mcp.example.test/docs", auth=ToolServerAuth.NONE, timeout_seconds=7.5
)
"""Two tool servers a deployment declares; the agent below names the first."""

SECRETS = ToolServerSecrets({GITHUB.id: "s3cret"})


def models(
    *,
    servers: Sequence[ToolServerConfig] = (),
    agent: AgentDefinition | None = None,
    **changes: Any,
) -> ModelsConfig:
    """A configuration with one provider, one model and one agent on LangChain."""
    model = ModelConfig(id=MODEL, provider=PROVIDER, name="claude-sonnet-5", **changes)
    return ModelsConfig(
        providers={PROVIDER: ANTHROPIC_PROVIDER},
        models={MODEL: model},
        agents={AGENT: agent or definition()},
        tool_servers={server.id: server for server in servers},
    )


def definition(**changes: Any) -> AgentDefinition:
    fields: dict[str, Any] = {
        "id": AGENT,
        "model": MODEL,
        "engine": Engine.LANGGRAPH,
        "system_prompt": SYSTEM_PROMPT,
    }
    return agent_definition(**{**fields, **changes})


def keys() -> ProviderKeys:
    return ProviderKeys({PROVIDER: KEY})


class ScriptedChatModel(BaseChatModel):
    """A chat model a test writes the answers for, one per call, and can stop or break.

    A real ``BaseChatModel`` with a real ``_astream``, so the framework's loop,
    its streaming and its releasing are exercised as they would be against a
    provider -- and so that a cancellation reaches an ``await`` the way it
    would reach a socket. ``open_streams`` is what "the engine released what
    it held" looks like from underneath: the ``finally`` of this generator is
    reached only when the stream is closed.

    ``responses`` is one list of chunks per call the framework makes: the
    first answer, then the answer after the tools ran, and so on. ``error``
    and ``gate`` apply to the **last** of them, which is where a provider that
    fails or hangs does so.
    """

    responses: list[list[Any]] = []
    """What each call streams, one ``AIMessageChunk`` content per item."""
    error: Any = None
    """Raised after the last response's chunks, which is how a provider fails mid-answer."""
    gate: Any = None
    """Awaited after the last response's chunks: a turn that hangs, for the cancellation test."""
    seen: list[list[BaseMessage]] = []
    """Every call's messages, exactly as the framework handed them over."""
    bound: list[Any] = []
    """Every call's tools, as ``bind_tools`` was handed them; ``None`` when none were."""
    calls: int = 0
    open_streams: int = 0

    model_config = {"arbitrary_types_allowed": True}

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> Any:
        """What the real client does: the definitions ride along as ``tools``."""
        return self.bind(tools=list(tools), **kwargs)

    def _chunk(self, content: Any) -> AIMessageChunk:
        """One scripted chunk: content, or a whole chunk written by the test."""
        if isinstance(content, AIMessageChunk):
            return content
        return AIMessageChunk(content=content, response_metadata=dict(ANTHROPIC))

    def _next(self, messages: list[BaseMessage], kwargs: dict[str, Any]) -> tuple[list[Any], bool]:
        """The chunks of this call, and whether it is the last scripted one."""
        self.seen.append(list(messages))
        self.bound.append(kwargs.get("tools"))
        at = self.calls
        self.calls += 1
        chunks = self.responses[at] if at < len(self.responses) else []
        return chunks, self.calls >= len(self.responses)

    def _generate(
        self, messages: list[BaseMessage], stop: Any = None, run_manager: Any = None, **kwargs: Any
    ) -> ChatResult:
        """The whole answer at once: what a model that does not stream does.

        A plain ``AIMessage`` and not a chunk -- the same **type** the real
        client's ``_generate`` returns, which is what LangGraph passes through
        its message stream unchanged.
        """
        chunks, last = self._next(messages, kwargs)
        merged = AIMessageChunk(content="", response_metadata=dict(ANTHROPIC))
        for content in chunks:
            merged = merged + self._chunk(content)
        if last and self.error is not None:
            raise self.error
        whole = AIMessage(
            content=merged.content,
            tool_calls=merged.tool_calls,
            response_metadata=dict(merged.response_metadata),
        )
        return ChatResult(generations=[ChatGeneration(message=whole)])

    async def _astream(
        self, messages: list[BaseMessage], stop: Any = None, run_manager: Any = None, **kwargs: Any
    ) -> AsyncIterator[ChatGenerationChunk]:
        chunks, last = self._next(messages, kwargs)
        self.open_streams += 1
        try:
            for content in chunks:
                chunk = ChatGenerationChunk(message=self._chunk(content))
                if run_manager is not None:
                    await run_manager.on_llm_new_token(chunk.text, chunk=chunk)
                yield chunk
            if last and self.error is not None:
                raise self.error
            if last and self.gate is not None:
                await self.gate.wait()
        finally:
            self.open_streams -= 1


def in_pieces(text: str, pieces: int = PIECES) -> list[str]:
    """``text`` cut into that many pieces, the way a provider sends one."""
    size = max(1, -(-len(text) // pieces))
    return [text[start : start + size] for start in range(0, len(text), size)]


ANTHROPIC = {"model_provider": "anthropic"}
"""What the real client puts on every chunk: the provider, by which
langchain-core normalises the vendor's blocks."""


def calling(call_id: str, name: str, arguments: dict[str, Any], *, index: int = 1) -> list[Any]:
    """The chunks the real client makes of one streamed tool call.

    A ``content_block_start`` lifts the call on to the message as a tool-call
    chunk with its id and name and no arguments; each ``input_json_delta``
    is a chunk with a piece of the JSON and no id. The vendor's own block
    rides along in the content, as it does in the client.
    """
    written = json.dumps(arguments)
    half = len(written) // 2
    return [
        AIMessageChunk(
            content=[
                {"type": "tool_use", "id": call_id, "name": name, "input": {}, "index": index}
            ],
            tool_call_chunks=[tool_call_chunk(index=index, id=call_id, name=name, args="")],
            response_metadata=dict(ANTHROPIC),
        ),
        *(
            AIMessageChunk(
                content=[{"type": "input_json_delta", "partial_json": piece, "index": index}],
                tool_call_chunks=[tool_call_chunk(index=index, id=None, name=None, args=piece)],
                response_metadata=dict(ANTHROPIC),
            )
            for piece in (written[:half], written[half:])
            if piece
        ),
    ]


def thinking(text: str) -> AIMessageChunk:
    """The chunk the real client makes of a piece of the model's thinking."""
    return AIMessageChunk(
        content=[{"type": "thinking", "thinking": text, "index": 0}],
        response_metadata=dict(ANTHROPIC),
    )


def _search(q: str) -> str:
    """Search for repositories."""
    if q == "boom":
        # What a server's tool that failed becomes in the framework's client:
        # a ``ToolException`` the tool handles, which is a result marked as an
        # error and not a failure of the turn.
        raise ToolException("no such repo")
    return "found 3"


search = StructuredTool.from_function(_search, name=SEARCH, handle_tool_error=True)


async def plain_tools(*_: object) -> Sequence[BaseTool]:
    """The one tool every scripted turn is handed, as a plain function.

    Exactly as the examples hand tools over: the framework runs it, and
    whether it came from a server or a function is nothing the loop can tell.
    """
    return [search]


def scripted(script: Script) -> ScriptedChatModel:
    """The chat model one turn of that script needs: one response per answer."""
    responses: list[list[Any]] = []
    for say in script.answers:
        chunks: list[Any] = in_pieces(say.text) if say.text else []
        for at, call in enumerate(say.calls, start=1):
            chunks.extend(calling(call.call_id, call.name, dict(call.arguments), index=at))
        responses.append(chunks)
    return ScriptedChatModel(
        responses=responses,
        disable_streaming=not all(say.streamed for say in script.answers),
        error=RuntimeError("the provider said no") if script.ending is Ending.FAIL else None,
        gate=asyncio.Event() if script.ending is Ending.HANG else None,
    )


class TestLangGraphAgent(AgentContract):
    """The real engine, held to everything the port promises."""

    can_answer_without_streaming = True
    """A model that does not stream hands the whole answer over, and so does the turn."""

    can_call_tools = True
    """The scripted turn is handed ``search``, as a plain function the framework runs."""

    def new_agent(self, script: Script) -> Agent:
        self.model = scripted(script)
        return LangGraphAgent(
            models(),
            keys(),
            chat_model_for=lambda *_: self.model,  # noqa: ARG005
            tools_for=plain_tools,
        )

    def held(self, agent: Agent) -> int:
        assert isinstance(agent, LangGraphAgent)
        # Both halves: the engine's own graph stream, and the model's stream
        # inside it. A turn that let go of the first and not the second would
        # still be holding a response open.
        return agent.held + self.model.open_streams

    def definition(self) -> AgentDefinition:
        return definition()


# --- what the model is given, and what comes back -------------------------


async def turn_of(
    agent: LangGraphAgent,
    *,
    prompt: str = PROMPT,
    definition_: AgentDefinition | None = None,
    model: str = MODEL,
    state: bytes | None = None,
) -> list[Event]:
    """Every event of one turn, run to its end."""
    seen: list[Event] = []
    async for event in agent.stream(definition_ or definition(), prompt, model=model, state=state):
        seen.append(event)
    return seen


def engine(model: ScriptedChatModel, **changes: Any) -> LangGraphAgent:
    return LangGraphAgent(
        models(**changes),
        keys(),
        SECRETS,
        chat_model_for=lambda *_: model,  # noqa: ARG005
        tools_for=plain_tools,
    )


def done(events: list[Event]) -> Done:
    (ended,) = [event for event in events if isinstance(event, Done)]
    return ended


def said(messages: Sequence[BaseMessage]) -> list[tuple[str, str]]:
    """The messages as a test compares them: the type and the text of each."""
    return [(message.type, message.text) for message in messages]


@asyncio_test
async def test_the_system_prompt_comes_first_and_is_not_a_message() -> None:
    model = ScriptedChatModel(responses=[["Someone who plays fair."]])

    await turn_of(engine(model))

    assert said(model.seen[0]) == [("system", SYSTEM_PROMPT), ("human", PROMPT)]


@asyncio_test
async def test_an_agent_with_no_system_prompt_sends_none() -> None:
    model = ScriptedChatModel(responses=[["Done."]])

    await turn_of(engine(model), definition_=definition(system_prompt=""))

    assert said(model.seen[0]) == [("human", PROMPT)]


@asyncio_test
async def test_the_memory_goes_in_before_the_prompt_and_comes_back_with_the_turn() -> None:
    """The memory is the framework's own messages, in its own encoding: what
    the last turn was done with goes in whole, the question after it, and
    what comes back is all of that and the answer -- for the next turn."""
    model = ScriptedChatModel(responses=[["And a robin sings."]])
    earlier = write_state([HumanMessage("Earlier?"), AIMessage("Earlier.")])

    seen = await turn_of(engine(model), state=earlier)

    assert said(model.seen[0]) == [
        ("system", SYSTEM_PROMPT),
        ("human", "Earlier?"),
        ("ai", "Earlier."),
        ("human", PROMPT),
    ]
    assert said(read_state(done(seen).state)) == [
        ("human", "Earlier?"),
        ("ai", "Earlier."),
        ("human", PROMPT),
        ("ai", "And a robin sings."),
    ]


def test_the_memory_round_trips_through_the_frameworks_encoding_and_none_is_nothing() -> None:
    messages = [HumanMessage("Earlier?"), AIMessage("Earlier.", tool_calls=[])]

    assert read_state(write_state(messages)) == messages
    assert read_state(None) == []


@pytest.mark.parametrize("state", [b"not json", b'{"messages": 1}', b"[1, 2]"], ids=repr)
@asyncio_test
async def test_a_memory_this_engine_did_not_write_fails_the_turn(state: bytes) -> None:
    # The platform hands an engine its own states only, so this is a fault --
    # reported where the stream is iterated, and holding nothing afterwards.
    agent = engine(ScriptedChatModel(responses=[["Never."]]))

    with pytest.raises(InvalidValueError, match="memory is not one this engine wrote"):
        await turn_of(agent, state=state)

    assert agent.held == 0


@asyncio_test
async def test_thinking_is_streamed_as_reasoning_and_kept_out_of_the_answer() -> None:
    model = ScriptedChatModel(responses=[[thinking("Hmm."), thinking(" Fair."), "An answer."]])

    seen = await turn_of(engine(model))

    assert seen == [
        ReasoningDelta(text="Hmm."),
        ReasoningDelta(text=" Fair."),
        TextDelta(text="An answer."),
        done(seen),
    ]
    assert done(seen).text == "An answer."


@asyncio_test
async def test_an_answer_with_nothing_in_it_is_done_with_nothing() -> None:
    model = ScriptedChatModel(responses=[[""]])

    seen = await turn_of(engine(model))

    assert seen == [done(seen)]
    assert done(seen).text == ""


@asyncio_test
async def test_a_model_that_does_not_stream_arrives_whole_with_no_delta() -> None:
    # A client whose `_stream` is not called streams nothing; LangGraph passes
    # the whole message through, and the answer is what the turn is done with.
    model = ScriptedChatModel(responses=[["All of it at once."]], disable_streaming=True)

    seen = await turn_of(engine(model))

    check_backend_events(seen)
    assert seen == [done(seen)]
    assert done(seen).text == "All of it at once."
    assert model.open_streams == 0


# --- the tools -------------------------------------------------------------


@asyncio_test
async def test_the_agents_servers_reach_the_framework_with_the_secrets_in_the_agents_order() -> (
    None
):
    handed: list[tuple[Sequence[ToolServerConfig], ToolServerSecrets]] = []

    async def record(servers: Sequence[ToolServerConfig], secrets: ToolServerSecrets) -> list[Any]:
        handed.append((servers, secrets))
        return []

    model = ScriptedChatModel(responses=[["Done."]])
    agent = LangGraphAgent(
        models(servers=(GITHUB, DOCS), agent=definition(tools=(DOCS.id, GITHUB.id))),
        keys(),
        SECRETS,
        chat_model_for=lambda *_: model,  # noqa: ARG005
        tools_for=record,
    )

    await turn_of(agent, definition_=definition(tools=(DOCS.id, GITHUB.id)))

    ((servers, secrets),) = handed
    assert list(servers) == [DOCS, GITHUB]
    assert secrets is SECRETS


@asyncio_test
async def test_the_tools_are_bound_to_the_model_as_the_framework_takes_them() -> None:
    model = ScriptedChatModel(responses=[["Done."]])

    await turn_of(engine(model))

    (bound,) = model.bound
    assert [tool_.name for tool_ in bound] == [SEARCH]


@asyncio_test
async def test_a_tool_that_fails_is_a_result_the_model_reads_and_not_a_failure_of_the_turn() -> (
    None
):
    """The framework's loop, not the platform's: a tool that raised is an
    error result, the model is asked again with it, and the turn goes on
    (``docs/specs/runs.md``, "Tools")."""
    model = ScriptedChatModel(
        responses=[calling("toolu_01", SEARCH, {"q": "boom"}), ["No such repository, then."]]
    )

    seen = await turn_of(engine(model))

    check_backend_events(seen)
    (result,) = [event for event in seen if isinstance(event, ToolResult)]
    assert (result.call_id, result.name, result.is_error) == ("toolu_01", SEARCH, True)
    assert "no such repo" in result.output
    assert done(seen).text == "No such repository, then."
    # The second call was made with the error in front of the model.
    assert [message.type for message in model.seen[1]] == ["system", "human", "ai", "tool"]


@asyncio_test
async def test_a_result_longer_than_a_part_is_cut_to_one_for_the_transcript_only() -> None:
    long = "x" * (MAX_PART_CHARS + 10)

    def _read(path: str) -> str:  # noqa: ARG001 - the model's argument
        """Read a file."""
        return long

    read = StructuredTool.from_function(_read, name="read")

    async def one_tool(*_: object) -> Sequence[BaseTool]:
        return [read]

    model = ScriptedChatModel(responses=[calling("toolu_01", "read", {"path": "a"}), ["Read."]])
    agent = LangGraphAgent(
        models(), keys(), chat_model_for=lambda *_: model, tools_for=one_tool  # noqa: ARG005
    )

    seen = await turn_of(agent)

    (result,) = [event for event in seen if isinstance(event, ToolResult)]
    assert result.output == long[:MAX_PART_CHARS]
    # What the model was sent is the framework's, whole.
    assert model.seen[1][-1].text == long


@asyncio_test
async def test_a_turn_that_never_stops_calling_tools_is_bounded_by_the_framework() -> None:
    # More rounds than the bound allows, each asking once more.
    model = ScriptedChatModel(
        responses=[
            calling(f"toolu_{at:02}", SEARCH, {"q": "robinauts"})
            for at in range(2 * MAX_TOOL_ROUNDS)
        ]
    )
    agent = engine(model)
    seen: list[Event] = []

    with pytest.raises(GraphRecursionError):
        async for event in agent.stream(definition(), PROMPT, model=MODEL, state=None):
            seen.append(event)

    check_backend_events(seen, cut_short=True)
    assert MAX_TOOL_ROUNDS <= len([event for event in seen if isinstance(event, ToolCall)])
    assert len([event for event in seen if isinstance(event, ToolCall)]) <= MAX_TOOL_ROUNDS + 1
    assert (agent.held, model.open_streams) == (0, 0)


# --- the context -------------------------------------------------------------


def a_long_history(messages: int = 30, chars: int = 200) -> bytes:
    """A memory past any small window: that many messages of that many characters."""
    return write_state(
        [
            (HumanMessage if at % 2 == 0 else AIMessage)(f"{at}: " + "word " * (chars // 5))
            for at in range(messages)
        ]
    )


@asyncio_test
async def test_a_history_past_the_window_is_summarised_and_the_summary_is_not_the_answer() -> None:
    """The middleware calls the model for a summary before the turn's own
    call, replaces the older messages with it in the memory handed back, and
    what it said is nowhere in what the turn streamed."""
    model = ScriptedChatModel(responses=[["A summary of all that."], ["The answer."]])

    seen = await turn_of(engine(model, context_window=1_000), state=a_long_history())

    check_backend_events(seen)
    assert [event.text for event in seen if isinstance(event, TextDelta)] == ["The answer."]
    assert done(seen).text == "The answer."
    assert model.calls == 2
    # The summary stands where the older messages were, and the memory is
    # shorter than what went in.
    remembered = read_state(done(seen).state)
    assert len(remembered) < 30
    assert any("A summary of all that." in message.text for message in remembered)


@asyncio_test
async def test_a_history_within_the_window_is_sent_as_it_is() -> None:
    model = ScriptedChatModel(responses=[["The answer."]])

    seen = await turn_of(engine(model), state=a_long_history())

    assert model.calls == 1
    assert len(read_state(done(seen).state)) == 32


def test_the_window_is_the_configured_one_then_the_frameworks_then_the_default() -> None:
    configured = ModelConfig(
        id=MODEL, provider=PROVIDER, name="claude-sonnet-5", context_window=5_000
    )
    known = ModelConfig(id=MODEL, provider=PROVIDER, name="claude-haiku-4-5")
    unknown = ModelConfig(id=MODEL, provider=PROVIDER, name="anthropic/claude-sonnet-5")

    assert context_window(ScriptedChatModel(), configured) == 5_000
    assert context_window(chat_model(known, ANTHROPIC_PROVIDER, KEY), known) == 200_000
    assert context_window(chat_model(unknown, ANTHROPIC_PROVIDER, KEY), unknown) == (
        DEFAULT_CONTEXT_WINDOW
    )
    assert context_window(ScriptedChatModel(), unknown) == DEFAULT_CONTEXT_WINDOW


def test_the_middleware_is_summarisation_then_the_vendors_cache() -> None:
    built = middleware(ScriptedChatModel(), ModelConfig(id=MODEL, provider=PROVIDER, name="c"))

    assert [type(one) for one in built] == [
        SummarizationMiddleware,
        AnthropicPromptCachingMiddleware,
    ]


# --- the tool servers, as the framework's client connects to them ------------


def test_a_bearer_server_is_reached_at_its_endpoint_with_its_secret_and_its_timeout() -> None:
    connection = mcp_connection(GITHUB, SECRETS)

    assert connection == {
        "transport": "streamable_http",
        "url": GITHUB.url,
        "headers": {"Authorization": "Bearer s3cret"},
        "timeout": GITHUB.timeout_seconds,
        "sse_read_timeout": GITHUB.timeout_seconds,
    }


def test_a_basic_server_sends_the_user_and_the_secret_as_the_scheme_says() -> None:
    server = ToolServerConfig(
        id="wiki",
        url="https://wiki.example.test/mcp",
        auth=ToolServerAuth.BASIC,
        user="robin",
        secret_env="WIKI_TOKEN",
    )
    pair = base64.b64encode(b"robin:s3cret").decode("ascii")

    connection = mcp_connection(server, ToolServerSecrets({"wiki": "s3cret"}))

    assert connection["headers"] == {"Authorization": f"Basic {pair}"}


def test_a_public_server_is_sent_no_credential() -> None:
    connection = mcp_connection(DOCS, SECRETS)

    assert connection["headers"] == {}
    assert connection["timeout"] == 7.5


@asyncio_test
async def test_the_engine_holds_nothing_after_a_turn_that_ended() -> None:
    model = ScriptedChatModel(responses=[["Done."]])
    agent = engine(model)

    await turn_of(agent)

    assert (agent.held, model.open_streams) == (0, 0)


# --- the model a turn runs on ------------------------------------------------

OTHER_MODEL = "opus"
"""A second model beside the agent's default, which a conversation may be on."""


def two_models() -> ModelsConfig:
    """The one agent, whose default is ``MODEL``, and ``OTHER_MODEL`` beside it."""
    config = models()
    other = ModelConfig(id=OTHER_MODEL, provider=PROVIDER, name="claude-opus-5")
    return ModelsConfig(
        providers=config.providers,
        models={**config.models, OTHER_MODEL: other},
        agents=config.agents,
    )


@asyncio_test
async def test_a_turn_runs_on_the_model_it_is_given_and_not_the_agent_s_default() -> None:
    # The run's model is the conversation's, which its author may have moved
    # off the agent's default: that is the one the client is built for.
    built: list[ModelConfig] = []

    def client_for(model: ModelConfig, *_: object) -> BaseChatModel:
        built.append(model)
        return ScriptedChatModel(responses=[["Done."]])

    agent = LangGraphAgent(two_models(), keys(), chat_model_for=client_for, tools_for=plain_tools)

    await turn_of(agent, model=OTHER_MODEL)

    assert [model.name for model in built] == ["claude-opus-5"]


@asyncio_test
async def test_a_model_the_configuration_does_not_have_fails_the_turn() -> None:
    # Where the stream is iterated, like any other failure of a turn, and
    # holding nothing afterwards.
    agent = engine(ScriptedChatModel(responses=[["Done."]]))
    events = agent.stream(definition(), PROMPT, model="gpt-5-5", state=None)

    with pytest.raises(UnknownModelError):
        await anext(events)

    assert agent.held == 0


# --- the client the configuration describes ---------------------------------


def test_anthropic_is_built_with_the_key_timeout_retries_and_ceiling() -> None:
    model = ModelConfig(
        id=MODEL,
        provider=PROVIDER,
        name="claude-sonnet-5",
        timeout_seconds=17.0,
        max_output_tokens=1234,
    )

    built = chat_model(model, ANTHROPIC_PROVIDER, KEY)

    assert isinstance(built, ChatAnthropic)
    assert built.model == "claude-sonnet-5"
    assert built.anthropic_api_key is not None
    assert built.anthropic_api_key.get_secret_value() == KEY
    assert built.default_request_timeout == 17.0
    assert built.max_retries == MAX_RETRIES
    assert built.max_tokens == 1234


def test_a_model_with_no_ceiling_gets_the_engine_s_own() -> None:
    # Anthropic's API requires one on every request, so the engine sends one.
    built = chat_model(ModelConfig(id=MODEL, provider=PROVIDER, name="c"), ANTHROPIC_PROVIDER, KEY)

    assert isinstance(built, ChatAnthropic)
    assert built.max_tokens == DEFAULT_ANTHROPIC_OUTPUT_TOKENS


def test_the_key_is_not_in_what_the_keys_print() -> None:
    printed = repr(keys())

    assert KEY not in printed
    assert PROVIDER in printed


REDIRECTING_VARIABLES = {
    # Both are read by ChatAnthropic, and the second by the SDK underneath it,
    # when no base URL is passed: one of them inherited from a shell, a unit
    # file or a container image would send every turn -- and the key -- to
    # whatever host it named.
    "ANTHROPIC_API_URL": "https://evil.example.test",
    "ANTHROPIC_BASE_URL": "https://evil.example.test",
    # Read by ChatAnthropic when no key is passed.
    "ANTHROPIC_API_KEY": "sk-somebody-elses-key",
    # A proxy only this one client would obey. An operator's proxy is
    # HTTPS_PROXY, which is how everything else in this process is proxied.
    "ANTHROPIC_PROXY": "http://evil.example.test:3128",
}
"""An environment doing everything it can to redirect a turn and its key."""


def test_the_endpoint_and_the_key_come_from_the_configuration_and_never_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name, value in REDIRECTING_VARIABLES.items():
        monkeypatch.setenv(name, value)

    built = chat_model(
        ModelConfig(id=MODEL, provider=PROVIDER, name="claude-sonnet-5"), ANTHROPIC_PROVIDER, KEY
    )

    assert isinstance(built, ChatAnthropic)
    assert built.anthropic_api_url == ANTHROPIC_ENDPOINT
    assert built.anthropic_api_key is not None
    assert built.anthropic_api_key.get_secret_value() == KEY
    assert built.anthropic_proxy is None
    # And the client underneath it, which has env fallbacks of its own.
    assert str(built._async_client.base_url).rstrip("/") == ANTHROPIC_ENDPOINT
    assert str(built._client.base_url).rstrip("/") == ANTHROPIC_ENDPOINT
    assert built._async_client.api_key == KEY


def test_an_anthropic_compatible_provider_is_reached_at_the_configured_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The kind that names a protocol: the address is the operator's, and it is
    # still the **configuration** that decides it and never the environment.
    for name, value in REDIRECTING_VARIABLES.items():
        monkeypatch.setenv(name, value)

    built = chat_model(
        ModelConfig(id=MODEL, provider=COMPATIBLE_PROVIDER.id, name="anthropic/claude-sonnet-5"),
        COMPATIBLE_PROVIDER,
        KEY,
    )

    assert isinstance(built, ChatAnthropic)
    assert built.anthropic_api_url == COMPATIBLE_ENDPOINT
    assert str(built._async_client.base_url).rstrip("/") == COMPATIBLE_ENDPOINT
    assert str(built._client.base_url).rstrip("/") == COMPATIBLE_ENDPOINT
    assert built._async_client.api_key == KEY
    assert built.anthropic_proxy is None
    assert built.max_retries == MAX_RETRIES


def test_the_endpoint_is_the_vendors_or_the_operators_and_there_is_no_third_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # `endpoint_of` is the whole of the choice, and this is the whole of it:
    # the pinned constant for the vendor's kind, the configured address for the
    # protocol's, whatever the environment has been told to say.
    for name, value in REDIRECTING_VARIABLES.items():
        monkeypatch.setenv(name, value)

    assert endpoint_of(ANTHROPIC_PROVIDER) == ANTHROPIC_ENDPOINT
    assert endpoint_of(COMPATIBLE_PROVIDER) == COMPATIBLE_ENDPOINT


def test_both_kinds_the_engine_offers_have_an_endpoint_and_nothing_else_does() -> None:
    # A kind added to `kinds` without a branch in `endpoint_of` would be a turn
    # sent to a guess; a branch without the kind would be unreachable.
    assert LangGraphAgent.kinds == {ProviderKind.ANTHROPIC, ProviderKind.ANTHROPIC_COMPATIBLE}


@pytest.mark.parametrize(
    "kind", sorted(frozenset(ProviderKind) - LangGraphAgent.kinds), ids=lambda kind: kind.value
)
def test_a_kind_this_engine_does_not_reach_is_refused_rather_than_guessed_at(
    kind: ProviderKind,
) -> None:
    # The configuration is held to `kinds` at start-up, so nothing should ever
    # get here. If something does -- a kind added to the set without a branch,
    # a caller that built a definition by hand -- it must be a refusal naming
    # the provider and never a turn sent to whatever endpoint happened to be
    # nearest.
    # `openai-compatible` is among them and needs its base_url like any other
    # kind that names a protocol, so the record is built the way that kind's
    # own rules require rather than one way for all of them.
    endpoint = COMPATIBLE_ENDPOINT if kind in KINDS_WITH_BASE_URL else None
    provider = ModelProviderConfig(id="vendor", kind=kind, api_key_env="K", base_url=endpoint)

    with pytest.raises(ConfigError) as raised:
        endpoint_of(provider)

    assert list(raised.value.problems) == [
        f"model_providers.vendor: this build of the LangGraph engine cannot reach"
        f" a {kind.value} provider"
    ]


# --- nothing phones home ----------------------------------------------------


TRACING_VARIABLES = {
    "LANGSMITH_TRACING": "true",
    "LANGCHAIN_TRACING_V2": "true",
    "LANGSMITH_API_KEY": "not-a-real-key",
    "LANGCHAIN_API_KEY": "not-a-real-key",
    # The version 1 tracer's variables. langchain-core does not ignore them:
    # with either set and v2 tracing off -- which is what this adapter makes
    # sure of -- it raises, so a process that inherited one would fail every
    # turn. They are what `force_tracing_off` takes out of the environment.
    "LANGCHAIN_TRACING": "true",
    "LANGCHAIN_HANDLER": "langchain",
}
"""An environment doing everything it can to turn hosted tracing on."""


@pytest.fixture
def asking_to_trace(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Every variable langsmith reads, set, and read afresh -- then forgotten.

    ``langsmith.utils.get_env_var`` caches, so a variable set after something
    has already asked for it would not be seen at all and a test that proved
    nothing would pass. The cache is cleared **again on the way out**, because
    a cached "true" that outlived the test would be an environment later tests
    could not see the end of.

    What ``force_tracing_off`` sets is **process-wide on purpose** and is put
    back here too: a test that left langsmith's global switch and context var
    where it found them is a test that says nothing about the next one, and
    one that left them turned off would hide a later engine that forgot to
    turn them off itself. Both are private to langsmith and are reached as
    such, which is the honest cost of checking a process-wide promise.
    """
    context = langsmith._internal._context
    was_global = context._GLOBAL_TRACING_ENABLED
    was_context = context._TRACING_ENABLED.get()
    for name, value in TRACING_VARIABLES.items():
        monkeypatch.setenv(name, value)
    langsmith.utils.get_env_var.cache_clear()
    try:
        yield
    finally:
        monkeypatch.undo()
        langsmith.utils.get_env_var.cache_clear()
        context._GLOBAL_TRACING_ENABLED = was_global
        context._TRACING_ENABLED.set(was_context)


@pytest.fixture(scope="module", autouse=True)
def no_client_at_the_end() -> Iterator[None]:
    """Nothing in this module builds a LangSmith client, first call or last.

    Asserted **after** every test of the module as well as by the test of its
    own below: the global is created on first use and kept, so the claim is
    about the whole run and not about the moment one test happened to look.
    """
    yield
    assert langsmith.run_trees._CLIENT is None


class _NeverBuilt:
    """Anything that tries to reach LangSmith. Building one fails the test."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise AssertionError("the engine reached LangSmith")


@asyncio_test
async def test_a_turn_attaches_no_tracer_however_loudly_the_environment_asks(
    monkeypatch: pytest.MonkeyPatch, asking_to_trace: None
) -> None:
    # `_configure` swallows a tracer that will not build, so a tracer that
    # refuses to exist is not enough on its own: the predicate langchain-core
    # decides by is what is asserted, and the stand-in is what would catch a
    # tracer attached some other way.
    monkeypatch.setattr(tracer_module, "LangChainTracer", _NeverBuilt)
    model = ScriptedChatModel(responses=[["Quietly."]])

    seen = await turn_of(engine(model))

    assert [event for event in seen if isinstance(event, TextDelta)]
    assert _tracing_v2_is_enabled() is False


def test_the_engine_turns_tracing_off_the_moment_it_is_built(
    asking_to_trace: None,
) -> None:
    LangGraphAgent(models(), keys(), chat_model_for=lambda *_: ScriptedChatModel())  # noqa: ARG005

    assert _tracing_v2_is_enabled() is False


def test_turning_tracing_off_takes_the_version_1_variables_out_of_the_environment(
    asking_to_trace: None,
) -> None:
    # Turning tracing off must not be a way of breaking a deployment:
    # langchain-core raises on every call when one of these is set and v2
    # tracing is off, which is exactly the state this adapter puts it in.
    LangGraphAgent(models(), keys(), chat_model_for=lambda *_: ScriptedChatModel())  # noqa: ARG005

    assert all(name not in os.environ for name in TRACING_VARIABLES_REMOVED)


def test_no_langsmith_client_has_been_built_in_this_process() -> None:
    """Importing the engine, building one and running turns opens no client.

    The global below is the only client langchain-core's tracer would use, and
    it is created on first use and kept. It being ``None`` after this module's
    import and every turn above is the whole claim: nothing here has talked to
    LangSmith, and nothing could have without building it.
    """
    assert langsmith.run_trees._CLIENT is None


CUSTOM_HEADERS = "ANTHROPIC_CUSTOM_HEADERS"
"""The one client variable an argument cannot override: headers are merged."""


def test_the_key_header_is_the_configured_key_whatever_the_environment_injects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The SDK merges this variable into the headers the caller passed, so one
    # line of it would replace the key header outright and a turn would be
    # spent on somebody else's key. The caller's header wins, and the engine
    # is a caller.
    monkeypatch.setenv(CUSTOM_HEADERS, f"{ANTHROPIC_KEY_HEADER}: sk-somebody-elses-key")

    built = chat_model(
        ModelConfig(id=MODEL, provider=PROVIDER, name="claude-sonnet-5"), ANTHROPIC_PROVIDER, KEY
    )

    assert isinstance(built, ChatAnthropic)
    sent = _headers_of(built)
    assert [value for name, value in sent if name.lower() == ANTHROPIC_KEY_HEADER] == [KEY]


def test_building_the_engine_takes_the_header_variable_out_of_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The other half: an argument cannot say "and nothing else", so the
    # variable itself goes when the engine is built at start-up.
    monkeypatch.setenv(CUSTOM_HEADERS, "anthropic-beta: nobody-configured-this")

    LangGraphAgent(models(), keys(), chat_model_for=lambda *_: ScriptedChatModel())  # noqa: ARG005

    assert all(name not in os.environ for name in CLIENT_VARIABLES_REMOVED)


def _headers_of(built: ChatAnthropic) -> list[tuple[str, str]]:
    """The headers the client would really send, built without sending one."""
    client = built._async_client
    request = client._build_request(
        anthropic._models.FinalRequestOptions.construct(
            method="post", url="/v1/messages", json_data={}
        )
    )
    return list(request.headers.multi_items())


# --- and nothing is written down ---------------------------------------------


LEAK_SECRET = "the-conversation-nobody-should-log"
"""Stands in for a message and a system prompt in the subprocess below."""

LEAK_KEY = "sk-the-key-nobody-should-log"

REQUEST = """
import anthropic
from robinauts.adapters import ProviderKeys
from robinauts.adapters.agents.langgraph import chat_model
from robinauts.domain import ModelConfig, ModelProviderConfig, ModelsConfig, ProviderKind

{built}

provider = ModelProviderConfig(id="anthropic", kind=ProviderKind.ANTHROPIC, api_key_env="K")
config = ModelConfig(id="m", provider="anthropic", name="claude")
built_model = chat_model(config, provider, {key!r})
# The SDK writes its "Request options" record here, on the way to the wire and
# before anything is sent: no network is needed to make it leak.
built_model._async_client._build_request(
    anthropic._models.FinalRequestOptions.construct(
        method="post",
        url="/v1/messages",
        json_data={{"system": {secret!r}, "messages": [{{"role": "user", "content": {secret!r}}}]}},
    )
)
"""
"""A whole request built in a fresh process, with ``ANTHROPIC_LOG`` set.

A subprocess because the leak happens at **import**: ``anthropic``, which
``langchain-anthropic`` is built on, reads the variable at the bottom of its
own ``__init__`` and puts its logger at ``DEBUG`` for the life of the process.
"""

BUILT = (
    "from robinauts.adapters.agents.langgraph import LangGraphAgent\n"
    "LangGraphAgent(ModelsConfig(), ProviderKeys({}))"
)
"""The one line under test: building the engine is what silences the SDK."""


def in_a_fresh_process(program: str) -> str:
    """Run that program with ``ANTHROPIC_LOG=debug`` and hand back its stderr."""
    finished = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        env={**os.environ, "ANTHROPIC_LOG": "debug", "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert finished.returncode == 0, finished.stdout + finished.stderr
    return finished.stderr


@pytest.mark.io  # it runs a subprocess
def test_a_request_is_never_written_to_a_log_however_the_sdk_was_asked_to() -> None:
    """``ANTHROPIC_LOG=debug`` does not put a conversation on standard error.

    The same promise and the same fix as the other engine
    (``quiet_client_logging``): both reach the same vendor SDK, so both have
    the same variable to answer.
    """
    quiet = in_a_fresh_process(REQUEST.format(built=BUILT, key=LEAK_KEY, secret=LEAK_SECRET))

    assert LEAK_SECRET not in quiet
    assert "Request options" not in quiet


@pytest.mark.io
def test_the_same_process_without_the_engine_really_does_leak_the_conversation() -> None:
    """The other half: the test above would notice."""
    leaked = in_a_fresh_process(REQUEST.format(built="", key=LEAK_KEY, secret=LEAK_SECRET))

    assert LEAK_SECRET in leaked
    assert "Request options" in leaked
    # And what a leak does **not** carry, shown against a real one: the key is
    # a header, and the record is the request's options without them.
    assert LEAK_KEY not in leaked


def test_the_shared_fixture_knows_every_logger_this_engine_pins() -> None:
    """``tests/conftest.py`` restores these for the whole suite, and cannot import them.

    It names them itself, because it is loaded for every test and importing an
    adapter there would make the whole suite import an agent framework. This
    is what keeps the two lists from drifting apart.
    """
    assert tuple(VENDOR_LOGGERS) == QUIET_CLIENT_LOGGERS


def test_building_the_engine_holds_the_vendor_loggers_at_warning() -> None:
    for name in QUIET_CLIENT_LOGGERS:
        logging.getLogger(name).setLevel(logging.DEBUG)

    LangGraphAgent(models(), keys(), chat_model_for=lambda *_: ScriptedChatModel())  # noqa: ARG005

    assert [logging.getLogger(name).getEffectiveLevel() for name in QUIET_CLIENT_LOGGERS] == [
        QUIET_CLIENT_LEVEL
    ] * len(QUIET_CLIENT_LOGGERS)


def test_a_logger_an_operator_silenced_further_is_left_where_it_is() -> None:
    logging.getLogger(QUIET_CLIENT_LOGGERS[0]).setLevel(logging.CRITICAL)

    LangGraphAgent(models(), keys(), chat_model_for=lambda *_: ScriptedChatModel())  # noqa: ARG005

    assert logging.getLogger(QUIET_CLIENT_LOGGERS[0]).level == logging.CRITICAL


def test_building_the_engine_takes_the_logging_variable_out_of_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_LOG", "debug")

    LangGraphAgent(models(), keys(), chat_model_for=lambda *_: ScriptedChatModel())  # noqa: ARG005

    assert "ANTHROPIC_LOG" in CLIENT_VARIABLES_REMOVED
    assert all(name not in os.environ for name in CLIENT_VARIABLES_REMOVED)


def a_request_carrying(secret: str) -> None:
    """Build a whole request with that text in it, and send nothing.

    The same call the subprocess above makes, in this process: it is where the
    SDK writes its record, and it happens before anything reaches a socket.
    """
    built = chat_model(ModelConfig(id=MODEL, provider=PROVIDER, name="c"), ANTHROPIC_PROVIDER, KEY)
    assert isinstance(built, ChatAnthropic)
    built._async_client._build_request(
        anthropic._models.FinalRequestOptions.construct(
            method="post",
            url="/v1/messages",
            json_data={"system": secret, "messages": [{"role": "user", "content": secret}]},
        )
    )


def test_a_root_logger_turned_up_later_gets_no_conversation(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The ordinary deployment: the variable unset, and the root moved later.

    The same promise and the same case as the other engine: with
    ``ANTHROPIC_LOG`` unset the vendor's loggers sit at ``NOTSET`` and are
    already effectively quiet, so what has to be pinned is their **own** level
    -- otherwise an operator turning their root logger up to ``DEBUG`` gets
    every message of every conversation on standard error.
    """
    LangGraphAgent(models(), keys(), chat_model_for=lambda *_: ScriptedChatModel())  # noqa: ARG005

    with caplog.at_level(logging.DEBUG):
        a_request_carrying(LEAK_SECRET)

    assert LEAK_SECRET not in caplog.text
    assert "Request options" not in caplog.text


def test_the_same_root_logger_leaks_it_when_no_engine_pinned_anything(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The other half: a process where nothing pinned them really does leak."""
    for name in QUIET_CLIENT_LOGGERS:
        logging.getLogger(name).setLevel(logging.NOTSET)

    with caplog.at_level(logging.DEBUG):
        a_request_carrying(LEAK_SECRET)

    assert LEAK_SECRET in caplog.text


def test_a_level_named_on_the_emitting_logger_itself_is_raised_too() -> None:
    """A ``dictConfig`` entry naming the child walks past a pin on the parent."""
    emitting = logging.getLogger("anthropic._base_client")
    emitting.setLevel(logging.DEBUG)

    LangGraphAgent(models(), keys(), chat_model_for=lambda *_: ScriptedChatModel())  # noqa: ARG005

    assert emitting.level == QUIET_CLIENT_LEVEL
    assert "anthropic._base_client" in QUIET_CLIENT_LOGGERS
