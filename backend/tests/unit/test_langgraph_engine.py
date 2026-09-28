# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The LangGraph engine: the contract suite, the mapping, and what it never does.

The engine is run for real -- its graph compiled, its stream consumed, its
``finally`` exercised -- over a chat model the test scripts. No network, no
key: what is replaced is the one seam the adapter has for it
(``LangGraphAgent(chat_model_for=...)``), and everything else is the code a
deployment runs.

Four subjects:

- the **contract** both engines are held to (``contracts/agents.py``), run
  here over the real engine for the first time;
- the **mapping**: what the model is given, and what its chunks become. A
  turn of this engine holds one answer and always streams -- a graph with one
  node calls the model once, and the framework hands the answer over in pieces
  however the provider sent it -- which is what the two declarations on the
  contract subclass say;
- the **client**: that a model's configuration reaches Anthropic's client as
  the key, the timeout, the retries and the ceiling it describes, asserted on
  the constructed object because constructing one reaches nothing;
- **nothing phones home**: with the environment asking loudly for tracing, a
  turn attaches no tracer and this process has never built a LangSmith client.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator, Iterator, Sequence
from typing import Any

import anthropic
import langsmith
import langsmith._internal._context
import langsmith.run_trees
import pytest
from langchain_anthropic import ChatAnthropic
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.messages.tool import tool_call_chunk
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.tracers import langchain as tracer_module
from langchain_core.tracers.context import _tracing_v2_is_enabled

from aio import asyncio_test
from conftest import VENDOR_LOGGERS
from contracts.agents import AgentContract, Ending, Script
from conversations import agent_definition, answer, question
from robinauts.adapters import ProviderKeys
from robinauts.adapters.agents.langgraph import (
    ANTHROPIC_ENDPOINT,
    ANTHROPIC_KEY_HEADER,
    BLOCKS_LEFT_OUT,
    CLIENT_VARIABLES_REMOVED,
    DEFAULT_ANTHROPIC_OUTPUT_TOKENS,
    MAX_RETRIES,
    QUIET_CLIENT_LEVEL,
    QUIET_CLIENT_LOGGERS,
    TRACING_VARIABLES_REMOVED,
    VENDOR,
    LangGraphAgent,
    chat_model,
    endpoint_of,
)
from robinauts.core import check_engine_events
from robinauts.domain import (
    KINDS_WITH_BASE_URL,
    MAX_EXTRAS_BYTES,
    NO_RESULT,
    AgentDefinition,
    AnswerCompleted,
    AnswerReasoningDelta,
    AnswerStarted,
    AnswerTextDelta,
    ConfigError,
    Engine,
    EngineEvent,
    Message,
    ModelConfig,
    ModelProviderConfig,
    ModelsConfig,
    ProviderKind,
    ReasoningPart,
    Role,
    TextPart,
    ToolCallArgumentsDelta,
    ToolCallCompleted,
    ToolCallPart,
    ToolCallStarted,
    ToolDefinition,
    ToolResultPart,
    UnknownModelError,
    UnsupportedContentError,
    WaitingOnTools,
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


def models(**changes: Any) -> ModelsConfig:
    """A configuration with one provider, one model and one agent on LangGraph."""
    model = ModelConfig(id=MODEL, provider=PROVIDER, name="claude-sonnet-5", **changes)
    return ModelsConfig(
        providers={PROVIDER: ANTHROPIC_PROVIDER},
        models={MODEL: model},
        agents={AGENT: definition()},
    )


def definition() -> AgentDefinition:
    return agent_definition(
        id=AGENT, model=MODEL, engine=Engine.LANGGRAPH, system_prompt=SYSTEM_PROMPT
    )


def keys() -> ProviderKeys:
    return ProviderKeys({PROVIDER: KEY})


class ScriptedChatModel(BaseChatModel):
    """A chat model a test writes the chunks for, and can stop or break.

    A real ``BaseChatModel`` with a real ``_astream``, so the engine's graph,
    its streaming and its releasing are exercised as they would be against a
    provider -- and so that a cancellation reaches an ``await`` the way it
    would reach a socket. ``open_streams`` is what "the engine released what
    it held" looks like from underneath: the ``finally`` of this generator is
    reached only when the stream is closed.
    """

    chunks: list[Any] = []
    """What ``_astream`` yields, one ``AIMessageChunk`` content each."""
    error: Any = None
    """Raised after the chunks, which is how a provider fails mid-answer."""
    gate: Any = None
    """Awaited after the chunks: a turn that hangs, for the cancellation test."""
    provider_metadata: dict[str, Any] = {}
    """What the chunks carry as ``response_metadata``; Anthropic names itself."""
    seen: list[list[BaseMessage]] = []
    """Every call's messages, exactly as the graph handed them over."""
    bound: list[Any] = []
    """Every call's tools, as ``bind_tools`` was handed them; ``None`` when none were."""
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
        return AIMessageChunk(content=content, response_metadata=dict(self.provider_metadata))

    def _generate(
        self, messages: list[BaseMessage], stop: Any = None, run_manager: Any = None, **kwargs: Any
    ) -> ChatResult:
        """The whole answer at once: what a model that does not stream does.

        A plain ``AIMessage`` and not a chunk -- the same **type** the real
        client's ``_generate`` returns, which is what LangGraph passes through
        its message stream unchanged. Its content is the merged chunks', with
        the stream's own keys still in it, where the real client's carries the
        parsed blocks; the engine reads ``tool_calls``, the thinking blocks
        and ``.text``, which are the same either way.
        """
        self.seen.append(list(messages))
        self.bound.append(kwargs.get("tools"))
        merged = AIMessageChunk(content="", response_metadata=dict(self.provider_metadata))
        for content in self.chunks:
            merged = merged + self._chunk(content)
        whole = AIMessage(
            content=merged.content,
            tool_calls=merged.tool_calls,
            response_metadata=dict(merged.response_metadata),
        )
        return ChatResult(generations=[ChatGeneration(message=whole)])

    async def _astream(
        self, messages: list[BaseMessage], stop: Any = None, run_manager: Any = None, **kwargs: Any
    ) -> AsyncIterator[ChatGenerationChunk]:
        self.seen.append(list(messages))
        self.bound.append(kwargs.get("tools"))
        self.open_streams += 1
        try:
            for content in self.chunks:
                chunk = ChatGenerationChunk(message=self._chunk(content))
                if run_manager is not None:
                    await run_manager.on_llm_new_token(chunk.text, chunk=chunk)
                yield chunk
            if self.error is not None:
                raise self.error
            if self.gate is not None:
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


def scripted(script: Script) -> ScriptedChatModel:
    """The chat model one turn of that script needs.

    A turn of this engine is one call to the model, so it is the script's
    first answer that is scripted; ``answers_per_turn`` below is what keeps
    the suite from asking for a second. An answer that calls tools streams
    its text and then each call as the real client would.
    """
    first = script.answers[0] if script.answers else None
    said = first.text if first else ""
    streams = first is None or first.streamed
    chunks: list[Any] = in_pieces(said) if said else []
    for at, call in enumerate(first.calls if first else ()):
        chunks.extend(calling(call.call_id, call.name, dict(call.arguments), index=at + 1))
    return ScriptedChatModel(
        chunks=chunks,
        disable_streaming=not streams,
        error=RuntimeError("the provider said no") if script.ending is Ending.FAIL else None,
        gate=asyncio.Event() if script.ending is Ending.HANG else None,
    )


class TestLangGraphAgent(AgentContract):
    """The real engine, held to everything the port promises."""

    answers_per_turn = 1
    """A graph with one node calls the model once, so a turn holds one answer."""

    can_answer_without_streaming = True
    """A model that does not stream hands the whole answer over, and so does the turn."""

    def new_agent(self, script: Script) -> Agent:
        self.model = scripted(script)
        return LangGraphAgent(
            models(), keys(), chat_model_for=lambda *_: self.model  # noqa: ARG005
        )

    def held(self, agent: Agent) -> int:
        assert isinstance(agent, LangGraphAgent)
        # Both halves: the engine's own graph stream, and the model's stream
        # inside it. A turn that let go of the first and not the second would
        # still be holding a response open.
        return agent.held + self.model.open_streams

    def definition(self) -> AgentDefinition:
        return definition()


# --- what the model is given ------------------------------------------------


async def turn_of(
    agent: LangGraphAgent,
    history: Sequence[Message],
    *,
    definition_: AgentDefinition | None = None,
    model: str = MODEL,
    tools: Sequence[ToolDefinition] = (),
) -> list[EngineEvent]:
    """Every event of one turn, run to its end."""
    seen: list[EngineEvent] = []
    events = agent.run_turn(definition_ or definition(), history, tools, model=model)
    async for event in events:
        seen.append(event)
    return seen


def engine(model: ScriptedChatModel, **changes: Any) -> LangGraphAgent:
    return LangGraphAgent(
        models(**changes), keys(), chat_model_for=lambda *_: model  # noqa: ARG005
    )


@asyncio_test
async def test_the_system_prompt_comes_first_and_is_not_a_message() -> None:
    model = ScriptedChatModel(chunks=["Someone who plays fair."])
    asked = question("What is a robinaut?")

    await turn_of(engine(model), (asked,))

    (given,) = model.seen
    assert [(type(message).__name__, message.content) for message in given] == [
        ("SystemMessage", SYSTEM_PROMPT),
        ("HumanMessage", "What is a robinaut?"),
    ]


@asyncio_test
async def test_an_agent_with_no_system_prompt_sends_none() -> None:
    # An empty system message is not the same as none, and some providers
    # refuse it.
    model = ScriptedChatModel(chunks=["Hello."])
    agent = LangGraphAgent(models(), keys(), chat_model_for=lambda *_: model)  # noqa: ARG005

    await turn_of(
        agent,
        (question(),),
        definition_=agent_definition(
            id=AGENT, model=MODEL, engine=Engine.LANGGRAPH, system_prompt=""
        ),
    )

    (given,) = model.seen
    assert [type(message).__name__ for message in given] == ["HumanMessage"]


@asyncio_test
async def test_the_history_keeps_its_roles_and_carries_no_reasoning() -> None:
    model = ScriptedChatModel(chunks=["Again."])
    asked = question("What is a robinaut?")
    replied = answer(
        asked,
        parts=(ReasoningPart("thinking about it"), TextPart("Someone who plays fair.")),
    )
    again = question("And a robin?", parent=replied)

    await turn_of(engine(model), (asked, replied, again))

    (given,) = model.seen
    assert [(type(message).__name__, message.content) for message in given] == [
        ("SystemMessage", SYSTEM_PROMPT),
        ("HumanMessage", "What is a robinaut?"),
        # The thinking of the previous turn is not sent back: this version
        # keeps none of it, and what is stored holds none either.
        ("AIMessage", "Someone who plays fair."),
        ("HumanMessage", "And a robin?"),
    ]
    assert all(message.role is not Role.TOOL for message in (asked, replied, again))


# --- what the model's chunks become -----------------------------------------


@asyncio_test
async def test_thinking_is_streamed_as_reasoning_and_kept_out_of_the_answer() -> None:
    # Anthropic's own content blocks, as they arrive from the client: a
    # thinking block, then text. langchain-core normalises the first to a
    # standard `reasoning` block, which is what the engine reads.
    model = ScriptedChatModel(
        chunks=[
            [{"type": "thinking", "thinking": "let me think", "index": 0}],
            [{"type": "text", "text": "Someone who ", "index": 1}],
            [{"type": "text", "text": "plays fair.", "index": 1}],
        ],
        provider_metadata=dict(ANTHROPIC),
    )

    seen = await turn_of(engine(model), (question(),))

    assert isinstance(seen[0], AnswerStarted)
    assert [event.text for event in seen if isinstance(event, AnswerReasoningDelta)] == [
        "let me think"
    ]
    assert [event.text for event in seen if isinstance(event, AnswerTextDelta)] == [
        "Someone who ",
        "plays fair.",
    ]
    completed = seen[-1]
    assert isinstance(completed, AnswerCompleted)
    # The thinking is shown as it arrives and is in none of the parts: what
    # `domain.kept_parts` would drop anyway must not be inside the text.
    assert completed.parts == (TextPart("Someone who plays fair."),)


@asyncio_test
async def test_an_answer_with_nothing_in_it_is_announced_and_completed_empty() -> None:
    # A model that streamed chunks holding no text and no thinking -- an empty
    # piece, or a kind of block this version does not carry. A message has
    # content, so "the agent answered with nothing" is recorded as one empty
    # part rather than as a turn that produced no answer.
    seen = await turn_of(engine(ScriptedChatModel(chunks=[""])), (question(),))

    assert seen == [AnswerStarted(), AnswerCompleted(parts=(TextPart(""),))]


@asyncio_test
async def test_a_character_split_across_two_chunks_is_put_back_together() -> None:
    # A provider splits where it likes; what is stored has to be storable.
    whole = "\N{ROCKET}"
    above = ord(whole) - 0x10000
    high, low = chr(0xD800 + (above >> 10)), chr(0xDC00 + (above & 0x3FF))
    model = ScriptedChatModel(chunks=[high, low])

    seen = await turn_of(engine(model), (question(),))

    assert seen[-1] == AnswerCompleted(parts=(TextPart(whole),))


@asyncio_test
async def test_a_model_that_does_not_stream_arrives_whole_with_no_delta() -> None:
    """The "not streamed" shape, as this engine really produces it.

    The model hands the whole answer over at once as a plain message,
    LangGraph passes it through its message stream unchanged -- not as a
    chunk -- and the engine builds the answer from the node's update: the
    turn is announced and completed with no delta, which the port allows
    (``can_answer_without_streaming``).
    """
    whole = "All of it at once."
    model = ScriptedChatModel(chunks=[whole], disable_streaming=True)

    seen = await turn_of(engine(model), (question(),))

    assert [type(event) for event in seen] == [AnswerStarted, AnswerCompleted]
    assert seen[-1] == AnswerCompleted(parts=(TextPart(whole),))


@asyncio_test
async def test_the_whole_path_reaches_the_model_and_ends_in_the_question() -> None:
    """This engine's context policy, for now: everything (ADR 0004). What is
    kept above the port is that the question being answered is whole in what
    the model sees; it is checked here, where the model can be asked."""
    model = ScriptedChatModel(chunks=["Three."])
    first = question("Are you sure?", seconds=0)
    said = answer(first, "a" * 5_000, seconds=1)
    second = question("r" * 5_000, parent=said, seconds=2)
    replied = answer(second, "b" * 5_000, seconds=3)
    # The question is the longest message, so a policy that cut the tail --
    # the one thing ADR 0004 forbids -- would be caught here and not passed.
    third = question("q" * 5_001, parent=replied, seconds=4)

    await turn_of(engine(model), (first, said, second, replied, third))

    (heard,) = model.seen
    told = [message for message in heard if message.type != "system"]
    assert [message.type for message in told] == ["human", "ai", "human", "ai", "human"]
    assert [str(message.content) for message in told] == [
        "Are you sure?",
        said.text,
        second.text,
        replied.text,
        third.text,
    ]


# --- tools ---------------------------------------------------------------------

SEARCH = ToolDefinition(
    name="github__search",
    description="Search the repositories this deployment may see.",
    input_schema={"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]},
    annotations={"readOnlyHint": True},
)


def turn_with_tools() -> tuple[Message, Message, Message]:
    """A question, the answer that called a tool with its signed blocks, the result."""
    asked = question("look it up", seconds=0)
    calling_ = answer(
        asked,
        seconds=1,
        parts=(TextPart("Let me look."), ToolCallPart("toolu_01", SEARCH.name, {"q": "robinauts"})),
        extras={
            VENDOR: {
                "thinking": [
                    {"type": "thinking", "thinking": "hm", "signature": "SIG"},
                    {"type": "redacted_thinking", "data": "OPAQUE"},
                ]
            }
        },
    )
    results = Message(
        id=uuid.uuid4(),
        conversation_id=asked.conversation_id,
        parent_id=calling_.id,
        role=Role.TOOL,
        parts=(ToolResultPart("toolu_01", "found 3", is_error=True),),
        created_at=calling_.created_at,
    )
    return asked, calling_, results


@asyncio_test
async def test_the_tools_a_run_has_are_bound_as_the_vendor_takes_them() -> None:
    model = ScriptedChatModel(chunks=["Found it."])

    await turn_of(engine(model), (question(),), tools=(SEARCH,))
    await turn_of(engine(model), (question(),))

    # Anthropic's own shape, passed through untouched: the full name, the
    # description and the server's schema as it stands. The annotations are
    # the platform's and are not sent.
    assert model.bound == [
        [
            {
                "name": "github__search",
                "description": "Search the repositories this deployment may see.",
                "input_schema": SEARCH.input_schema,
            }
        ],
        None,
    ]


@asyncio_test
async def test_a_model_that_asks_for_a_tool_yields_the_call_and_ends_the_turn_waiting() -> None:
    """The call is announced with its id and name, its arguments stream as the
    model writes them, it completes as the platform's part, the answer holds
    it and the turn ends waiting: the engine runs nothing
    (``docs/specs/runs.md``)."""
    model = ScriptedChatModel(
        chunks=[
            AIMessageChunk(content=[{"type": "text", "text": "Let me look. ", "index": 0}]),
            *calling("toolu_01", "github__search", {"q": "robinauts"}, index=1),
            *calling("toolu_02", "jira__find", {}, index=2),
        ]
    )
    agent = engine(model)

    seen = await turn_of(agent, (question(),), tools=(SEARCH,))

    check_engine_events(seen)
    assert [type(event) for event in seen] == [
        AnswerStarted,
        AnswerTextDelta,
        ToolCallStarted,
        ToolCallArgumentsDelta,
        ToolCallArgumentsDelta,
        ToolCallCompleted,
        ToolCallStarted,
        ToolCallArgumentsDelta,
        ToolCallArgumentsDelta,
        ToolCallCompleted,
        AnswerCompleted,
        WaitingOnTools,
    ]
    assert seen[2] == ToolCallStarted(call_id="toolu_01", name="github__search")
    assert "".join(
        event.text
        for event in seen
        if isinstance(event, ToolCallArgumentsDelta) and event.call_id == "toolu_01"
    ) == json.dumps({"q": "robinauts"})
    first = ToolCallPart("toolu_01", "github__search", {"q": "robinauts"})
    second = ToolCallPart("toolu_02", "jira__find", {})
    assert seen[5] == ToolCallCompleted(call=first)
    assert seen[-2] == AnswerCompleted(parts=(TextPart("Let me look. "), first, second))
    assert (agent.held, model.open_streams) == (0, 0)


@asyncio_test
async def test_an_answer_that_only_calls_holds_the_calls_and_no_text() -> None:
    model = ScriptedChatModel(chunks=calling("toolu_01", "github__search", {"q": "x"}))

    seen = await turn_of(engine(model), (question(),), tools=(SEARCH,))

    check_engine_events(seen)
    assert seen[-2] == AnswerCompleted(
        parts=(ToolCallPart("toolu_01", "github__search", {"q": "x"}),)
    )
    assert isinstance(seen[-1], WaitingOnTools)


@asyncio_test
async def test_an_answer_that_was_not_streamed_still_yields_its_calls() -> None:
    # A model that does not stream hands the whole message over: nothing
    # comes through the stream, so the calls are announced and completed from
    # the node's update in one breath, with the arguments the framework
    # parsed, and the answer holds the same parts a streamed one would.
    model = ScriptedChatModel(
        chunks=[
            AIMessageChunk(
                content=[{"type": "text", "text": "Whole. ", "index": 0}],
                response_metadata=dict(ANTHROPIC),
            ),
            *calling("toolu_01", "github__search", {"q": "x"}),
        ],
        disable_streaming=True,
    )

    seen = await turn_of(engine(model), (question(),), tools=(SEARCH,))

    check_engine_events(seen)
    assert [type(event) for event in seen] == [
        AnswerStarted,
        ToolCallStarted,
        ToolCallCompleted,
        AnswerCompleted,
        WaitingOnTools,
    ]
    assert seen[-2] == AnswerCompleted(
        parts=(TextPart("Whole. "), ToolCallPart("toolu_01", "github__search", {"q": "x"}))
    )


@asyncio_test
async def test_arguments_that_are_not_json_or_not_an_object_fail_the_turn() -> None:
    for pieces in ("not json", '["a", "list"]'):
        model = ScriptedChatModel(
            chunks=[
                AIMessageChunk(
                    content=[
                        {
                            "type": "tool_use",
                            "id": "t1",
                            "name": "github__search",
                            "input": {},
                            "index": 0,
                        }
                    ],
                    tool_call_chunks=[
                        tool_call_chunk(index=0, id="t1", name="github__search", args="")
                    ],
                ),
                AIMessageChunk(
                    content=[{"type": "input_json_delta", "partial_json": pieces, "index": 0}],
                    tool_call_chunks=[tool_call_chunk(index=0, id=None, name=None, args=pieces)],
                ),
            ]
        )
        with pytest.raises(UnsupportedContentError, match="arguments"):
            await turn_of(engine(model), (question(),), tools=(SEARCH,))


@asyncio_test
async def test_a_call_the_client_did_not_lift_off_the_stream_is_refused_not_dropped() -> None:
    # The vendor's own block with no tool-call chunk beside it: a client that
    # did not recognise a call. Finishing the turn without it would store
    # half an answer that looks whole, so it is refused.
    model = ScriptedChatModel(
        chunks=[[{"type": "tool_use", "name": "search", "input": {}, "id": "t1", "index": 0}]]
    )

    with pytest.raises(UnsupportedContentError, match="did not translate"):
        await turn_of(engine(model), (question(),), tools=(SEARCH,))


@asyncio_test
async def test_arguments_for_no_announced_call_are_refused() -> None:
    model = ScriptedChatModel(
        chunks=[
            AIMessageChunk(
                content=[{"type": "input_json_delta", "partial_json": "{}", "index": 0}],
                tool_call_chunks=[tool_call_chunk(index=0, id=None, name=None, args="{}")],
            )
        ]
    )

    with pytest.raises(UnsupportedContentError, match="no tool call"):
        await turn_of(engine(model), (question(),), tools=(SEARCH,))


@asyncio_test
async def test_a_turn_with_tools_in_its_history_is_translated_for_the_model() -> None:
    """The answer's calls travel as the framework's tool calls with the
    vendor's signed blocks in front, and the tool message becomes one tool
    result per call, its error flag with it -- which the vendor's client
    formats as Anthropic wants them back."""
    from langchain_anthropic.chat_models import _format_messages

    asked, calling_, results = turn_with_tools()
    model = ScriptedChatModel(chunks=["Found three."])

    await turn_of(engine(model), (asked, calling_, results))

    (heard,) = model.seen
    _system, human, assistant, tool = heard
    assert human.type == "human" and str(human.content) == "look it up"
    assert assistant.type == "ai"
    assert assistant.tool_calls == [
        {
            "name": "github__search",
            "args": {"q": "robinauts"},
            "id": "toolu_01",
            "type": "tool_call",
        }
    ]
    assert assistant.content == [
        {"type": "thinking", "thinking": "hm", "signature": "SIG"},
        {"type": "redacted_thinking", "data": "OPAQUE"},
        {"type": "text", "text": "Let me look."},
    ]
    assert tool.type == "tool"
    assert (tool.content, tool.tool_call_id, tool.status) == ("found 3", "toolu_01", "error")
    # And what the vendor is sent, block for block.
    _, formatted = _format_messages(heard)
    assert formatted[1]["content"] == [
        {"type": "thinking", "thinking": "hm", "signature": "SIG"},
        {"type": "redacted_thinking", "data": "OPAQUE"},
        {"type": "text", "text": "Let me look."},
        {
            "type": "tool_use",
            "name": "github__search",
            "input": {"q": "robinauts"},
            "id": "toolu_01",
        },
    ]
    assert formatted[2] == {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "content": "found 3",
                "tool_use_id": "toolu_01",
                "is_error": True,
            }
        ],
    }


@asyncio_test
async def test_a_call_no_tool_message_answers_is_answered_with_what_the_record_says() -> None:
    """A stopped or failed tool round leaves the calls stored with no result
    under them; a question asked after it hangs under that answer. The vendor
    refuses a call with nothing answering it, so the model is told no result
    of the call was recorded -- and the record is not touched
    (``domain.NO_RESULT``)."""
    model = ScriptedChatModel(chunks=["Sorry, once more."])
    asked, calling_, _ = turn_with_tools()
    again = question("and now?", parent=calling_, seconds=2)

    await turn_of(engine(model), (asked, calling_, again), tools=(SEARCH,))

    (heard,) = model.seen
    assert [message.type for message in heard] == ["system", "human", "ai", "tool", "human"]
    assert (heard[3].tool_call_id, heard[3].content, heard[3].status) == (
        "toolu_01",
        NO_RESULT,
        "error",
    )
    # And the vendor's own mapping takes it: a tool_use answered by an error,
    # in the one user turn the question that followed is folded into.
    from langchain_anthropic.chat_models import _format_messages

    _system, sent = _format_messages(heard)
    assert sent[2]["content"] == [
        {"type": "tool_result", "tool_use_id": "toolu_01", "content": NO_RESULT, "is_error": True},
        {"type": "text", "text": "and now?"},
    ]


@asyncio_test
async def test_the_signed_blocks_are_replayed_to_the_model_that_made_them_and_to_no_other() -> None:
    asked, calling_, results = turn_with_tools()
    model = ScriptedChatModel(chunks=["Found three."])
    other = answer(asked, "plain", seconds=1, extras=calling_.extras)

    agent = LangGraphAgent(two_models(), keys(), chat_model_for=lambda *_: model)  # noqa: ARG005

    await turn_of(agent, (asked, calling_, results), model=OTHER_MODEL)
    await turn_of(agent, (asked, other))

    moved, same_model = model.seen
    # Moved to another model: the calls and the text go, the blocks do not.
    assert moved[2].content == [{"type": "text", "text": "Let me look."}]
    assert moved[2].tool_calls[0]["id"] == "toolu_01"
    # A plain answer on the model that made the blocks replays them too.
    assert same_model[2].content[0] == {"type": "thinking", "thinking": "hm", "signature": "SIG"}


@asyncio_test
async def test_the_vendors_signed_blocks_come_out_in_extras_and_the_thinking_is_streamed() -> None:
    model = ScriptedChatModel(
        chunks=[
            AIMessageChunk(
                content=[{"type": "thinking", "thinking": "let me ", "signature": "", "index": 0}],
                response_metadata=dict(ANTHROPIC),
            ),
            AIMessageChunk(
                content=[{"type": "thinking", "thinking": "think", "index": 0}],
                response_metadata=dict(ANTHROPIC),
            ),
            AIMessageChunk(
                content=[{"type": "thinking", "signature": "SIG", "index": 0}],
                response_metadata=dict(ANTHROPIC),
            ),
            AIMessageChunk(
                content=[{"type": "redacted_thinking", "data": "OPAQUE", "index": 1}],
                response_metadata=dict(ANTHROPIC),
            ),
            AIMessageChunk(
                content=[{"type": "text", "text": "Hi.", "index": 2}],
                response_metadata=dict(ANTHROPIC),
            ),
        ]
    )

    seen = await turn_of(engine(model), (question(),))

    check_engine_events(seen)
    assert [event.text for event in seen if isinstance(event, AnswerReasoningDelta)] == [
        "let me ",
        "think",
    ]
    assert seen[-1] == AnswerCompleted(
        parts=(TextPart("Hi."),),
        extras={
            VENDOR: {
                "thinking": [
                    {"type": "thinking", "thinking": "let me think", "signature": "SIG"},
                    {"type": "redacted_thinking", "data": "OPAQUE"},
                ]
            }
        },
    )


@asyncio_test
async def test_blocks_that_do_not_fit_extras_are_left_out_with_a_line_in_the_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    model = ScriptedChatModel(
        chunks=[
            AIMessageChunk(
                content=[
                    {
                        "type": "thinking",
                        "thinking": "x" * MAX_EXTRAS_BYTES,
                        "signature": "S",
                        "index": 0,
                    }
                ],
                response_metadata=dict(ANTHROPIC),
            ),
            AIMessageChunk(
                content=[{"type": "text", "text": "Hi.", "index": 1}],
                response_metadata=dict(ANTHROPIC),
            ),
        ]
    )

    with caplog.at_level(logging.WARNING):
        seen = await turn_of(engine(model), (question(),))

    assert seen[-1] == AnswerCompleted(parts=(TextPart("Hi."),))
    assert any(BLOCKS_LEFT_OUT in record.getMessage() for record in caplog.records)


@asyncio_test
async def test_the_engine_holds_nothing_after_a_turn_that_ended() -> None:
    model = ScriptedChatModel(chunks=["Done."])
    agent = engine(model)

    await turn_of(agent, (question(),))

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
        return ScriptedChatModel(chunks=["Done."])

    agent = LangGraphAgent(two_models(), keys(), chat_model_for=client_for)

    await turn_of(agent, (question(),), model=OTHER_MODEL)

    assert [model.name for model in built] == ["claude-opus-5"]


@asyncio_test
async def test_a_model_the_configuration_does_not_have_fails_the_turn() -> None:
    # Where the stream is iterated, like any other failure of a turn, and
    # holding nothing afterwards.
    agent = engine(ScriptedChatModel(chunks=["Done."]))
    events = agent.run_turn(definition(), (question(),), (), model="gpt-5-5")

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
    model = ScriptedChatModel(chunks=["Quietly."])

    seen = await turn_of(engine(model), (question(),))

    assert [event for event in seen if isinstance(event, AnswerTextDelta)]
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
