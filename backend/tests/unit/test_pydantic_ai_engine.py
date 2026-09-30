# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The Pydantic AI engine: the contract suite, the mapping, and what it never does.

The engine is run for real -- its framework agent built, its loop run by the
framework, its stream consumed, its ``finally`` exercised -- over one of
Pydantic AI's own test models, scripted by the test, and tools that are plain
functions. No network, no key: what is replaced is the two seams the adapter
has for them (``PydanticAIAgent(model_for=..., toolsets_for=...)``), and
everything else is the code a deployment runs.

Five subjects, the same five the LangChain engine's module has:

- the **contract** both engines are held to (``contracts/agents.py``), run
  here over the second real engine;
- the **mapping**: what the model is given -- the instructions, the memory,
  the tools, the settings -- and what the framework's stream becomes:
  deltas, calls, results, and the memory handed back;
- the **loop and the context**, which are the framework's: a tool that fails
  is a result the model reads, a turn that never stops is bounded, and a
  history past the window is trimmed by whole exchanges;
- the **client**: that a model's configuration reaches Anthropic's client as
  the key, the endpoint, the retries and the headers it describes, and a
  tool server's reaches the framework's MCP client as the endpoint, the
  credential and the timeout, asserted on the constructed object because
  constructing one reaches nothing;
- **nothing phones home**: with the environment asking loudly for tracing and
  every socket blocked, a whole turn runs, instruments nothing, and opens
  nothing.
"""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import logging
import os
import socket
import subprocess
import sys
from collections.abc import AsyncIterator, Iterator, Sequence
from datetime import timedelta
from typing import Any

import anthropic
import pydantic_ai
import pydantic_ai.agent
import pytest
from pydantic_ai import Agent as FrameworkAgent
from pydantic_ai import ModelRetry
from pydantic_ai.exceptions import ToolFailed, UnexpectedModelBehavior, UsageLimitExceeded
from pydantic_ai.mcp import MCPToolset
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models import Model
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.function import AgentInfo, DeltaThinkingPart, DeltaToolCall, FunctionModel
from pydantic_ai.toolsets import FunctionToolset, PrefixedToolset

from aio import asyncio_test
from conftest import VENDOR_LOGGERS
from contracts.agents import PROMPT, SEARCH, AgentContract, Ending, Script
from conversations import agent_definition
from robinauts.adapters import ProviderKeys, ToolServerSecrets
from robinauts.adapters.agents.pydantic_ai import (
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
    PydanticAIAgent,
    chat_model,
    context_window,
    endpoint_of,
    mcp_toolset,
    read_state,
    settings,
    within,
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
    """A configuration with one provider, one model and one agent on Pydantic AI."""
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
        "engine": Engine.PYDANTIC_AI,
        "system_prompt": SYSTEM_PROMPT,
    }
    return agent_definition(**{**fields, **changes})


def keys() -> ProviderKeys:
    return ProviderKeys({PROVIDER: KEY})


class ScriptedModel:
    """A model a test writes the answers for, one per call, and can stop or break.

    A real ``FunctionModel`` underneath -- ``model`` is what the engine is
    handed -- so the framework's agent, its loop, its streaming and its
    releasing are exercised as they would be against a provider, and so that a
    cancellation reaches an ``await`` the way it would reach a socket.
    ``open_streams`` is what "the engine released what it held" looks like
    from underneath: the ``finally`` of the generator below is reached only
    when the stream is closed.

    ``responses`` is one list of items per call the framework makes: the
    first answer, then the answer after the tools ran, and so on. Items are
    yielded as Pydantic AI's stream functions yield them: a ``str`` is text,
    and a mapping of ``DeltaThinkingPart`` or ``DeltaToolCall`` is thinking or
    a tool call. ``error`` and ``gate`` apply to the **last** response, which
    is where a provider that fails or hangs does so.
    """

    def __init__(
        self,
        *responses: list[Any],
        error: BaseException | None = None,
        gate: asyncio.Event | None = None,
    ) -> None:
        self.responses = list(responses)
        self.error = error
        self.gate = gate
        self.calls = 0
        self.seen: list[list[ModelMessage]] = []
        """Every call's messages, exactly as the framework handed them over."""
        self.instructions: list[str | None] = []
        """The instructions of every call: where the system prompt arrives."""
        self.settings: list[Any] = []
        """The model settings of every call: the timeout, the ceiling, the cache."""
        self.bound: list[list[str]] = []
        """The tools every call was declared, by name."""
        self.open_streams = 0
        self.model: Model = FunctionModel(stream_function=self._stream, model_name="scripted")

    async def _stream(self, messages: list[Any], info: AgentInfo) -> AsyncIterator[Any]:
        self.seen.append(list(messages))
        self.instructions.append(info.instructions)
        self.settings.append(info.model_settings)
        self.bound.append([tool.name for tool in info.function_tools])
        at = self.calls
        self.calls += 1
        items = self.responses[at] if at < len(self.responses) else []
        last = self.calls >= len(self.responses)
        self.open_streams += 1
        try:
            # A stream function must yield at least one item, so an answer
            # with nothing in it is one empty piece of text -- which is also
            # how a provider that said nothing arrives.
            for item in items or [""]:
                yield item
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


def calling(call_id: str, name: str, arguments: dict[str, Any], *, index: int = 1) -> list[Any]:
    """The items the function model takes for one streamed tool call.

    The first carries the name, the id and the first half of the JSON, which
    the framework's parts manager turns into the part starting; the second is
    the rest as a delta with no name.
    """
    written = json.dumps(arguments)
    half = len(written) // 2
    items: list[Any] = [
        {index: DeltaToolCall(name=name, json_args=written[:half], tool_call_id=call_id)}
    ]
    if written[half:]:
        items.append({index: DeltaToolCall(json_args=written[half:])})
    return items


def thinking(text: str, *, index: int = 0) -> dict[int, DeltaThinkingPart]:
    """One piece of the model's thinking, as the function model takes it."""
    return {index: DeltaThinkingPart(content=text)}


def search(q: str) -> str:
    """Search for repositories."""
    if q == "boom":
        # What a server's tool that failed becomes in the framework's client
        # (``mcp_toolset``): a failed result the model reads, and not a
        # failure of the turn.
        raise ToolFailed("no such repo")
    if q == "again":
        raise ModelRetry("try again")
    return "found 3"


def plain_tools(*_: object) -> Sequence[FunctionToolset[Any]]:
    """The one tool every scripted turn is handed, as a plain function.

    Exactly as the examples hand tools over: the framework runs it, and
    whether it came from a server or a function is nothing the loop can tell.
    """
    return [FunctionToolset([search])]


def scripted(script: Script) -> ScriptedModel:
    """The model one turn of that script needs: one response per answer."""
    responses: list[list[Any]] = []
    for say in script.answers:
        items: list[Any] = (in_pieces(say.text) if say.streamed else [say.text]) if say.text else []
        for at, call in enumerate(say.calls, start=1):
            items.extend(calling(call.call_id, call.name, dict(call.arguments), index=at))
        responses.append(items)
    return ScriptedModel(
        *responses,
        error=RuntimeError("the provider said no") if script.ending is Ending.FAIL else None,
        gate=asyncio.Event() if script.ending is Ending.HANG else None,
    )


class TestPydanticAIAgent(AgentContract):
    """The real engine, held to everything the port promises."""

    can_answer_without_streaming = False
    """Pydantic AI hands the answer over in pieces even when the model sent one."""

    can_call_tools = True
    """The scripted turn is handed ``search``, as a plain function the framework runs."""

    def new_agent(self, script: Script) -> Agent:
        self.model = scripted(script)
        return PydanticAIAgent(
            models(),
            keys(),
            model_for=lambda *_: self.model.model,  # noqa: ARG005
            toolsets_for=plain_tools,
        )

    def held(self, agent: Agent) -> int:
        assert isinstance(agent, PydanticAIAgent)
        # Both halves: the engine's own turn, and the model's stream inside it.
        # A turn that let go of the first and not the second would still be
        # holding a response open.
        return agent.held + self.model.open_streams

    def definition(self) -> AgentDefinition:
        return definition()


# --- what the model is given, and what comes back -------------------------


async def turn_of(
    agent: PydanticAIAgent,
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


def engine(model: ScriptedModel, **changes: Any) -> PydanticAIAgent:
    return PydanticAIAgent(
        models(**changes),
        keys(),
        SECRETS,
        model_for=lambda *_: model.model,  # noqa: ARG005
        toolsets_for=plain_tools,
    )


def done(events: list[Event]) -> Done:
    (ended,) = [event for event in events if isinstance(event, Done)]
    return ended


def said(messages: Sequence[ModelMessage]) -> list[tuple[str, str]]:
    """The messages as a test compares them: each part's kind and its text."""
    return [
        (part.part_kind, str(getattr(part, "content", "")))
        for message in messages
        for part in message.parts
    ]


def question(text: str) -> ModelRequest:
    return ModelRequest(parts=[UserPromptPart(content=text)])


def answer(text: str) -> ModelResponse:
    return ModelResponse(parts=[TextPart(content=text)])


@asyncio_test
async def test_the_system_prompt_is_an_instruction_and_not_one_of_the_messages() -> None:
    model = ScriptedModel(["Someone who plays fair."])

    await turn_of(engine(model))

    assert model.instructions == [SYSTEM_PROMPT]
    assert said(model.seen[0]) == [("user-prompt", PROMPT)]


@asyncio_test
async def test_an_agent_with_no_system_prompt_sends_no_instruction() -> None:
    model = ScriptedModel(["Done."])

    await turn_of(engine(model), definition_=definition(system_prompt=""))

    assert model.instructions == [None]


@asyncio_test
async def test_the_memory_goes_in_before_the_prompt_and_comes_back_with_the_turn() -> None:
    """The memory is the framework's own messages, in its own encoding: what
    the last turn was done with goes in whole, the question after it, and
    what comes back is all of that and the answer -- for the next turn."""
    model = ScriptedModel(["And a robin sings."])
    earlier = write_state([question("Earlier?"), answer("Earlier.")])

    seen = await turn_of(engine(model), state=earlier)

    assert said(model.seen[0]) == [
        ("user-prompt", "Earlier?"),
        ("text", "Earlier."),
        ("user-prompt", PROMPT),
    ]
    assert said(read_state(done(seen).state)) == [
        ("user-prompt", "Earlier?"),
        ("text", "Earlier."),
        ("user-prompt", PROMPT),
        ("text", "And a robin sings."),
    ]


def test_the_memory_round_trips_through_the_frameworks_encoding_and_none_is_nothing() -> None:
    messages = [question("Earlier?"), answer("Earlier.")]

    assert said(read_state(write_state(messages))) == said(messages)
    assert read_state(None) == []


@pytest.mark.parametrize("state", [b"not json", b'{"messages": 1}', b"[1, 2]"], ids=repr)
@asyncio_test
async def test_a_memory_this_engine_did_not_write_fails_the_turn(state: bytes) -> None:
    # The platform hands an engine its own states only, so this is a fault --
    # reported where the stream is iterated, and holding nothing afterwards.
    agent = engine(ScriptedModel(["Never."]))

    with pytest.raises(InvalidValueError, match="memory is not one this engine wrote"):
        await turn_of(agent, state=state)

    assert agent.held == 0


@asyncio_test
async def test_the_timeout_the_ceiling_and_the_cache_travel_with_the_request() -> None:
    model = ScriptedModel(["Done."])

    await turn_of(engine(model, timeout_seconds=17.0, max_output_tokens=1234))

    (sent,) = model.settings
    assert (sent["timeout"], sent["max_tokens"]) == (17.0, 1234)
    assert sent["anthropic_cache_instructions"] is True
    assert sent["anthropic_cache_tool_definitions"] is True
    assert sent["anthropic_cache"] is True


@asyncio_test
async def test_a_model_with_no_ceiling_configured_is_sent_the_engine_s_own() -> None:
    model = ScriptedModel(["Done."])

    await turn_of(engine(model))

    assert model.settings[0]["max_tokens"] == DEFAULT_ANTHROPIC_OUTPUT_TOKENS


def test_a_gateway_is_asked_for_the_cache_the_way_a_gateway_takes_it() -> None:
    # The vendor's own automatic breakpoint at the vendor; a breakpoint on the
    # last message at an endpoint that speaks the protocol without it.
    config = ModelConfig(id=MODEL, provider=PROVIDER, name="c")

    at_the_vendor = settings(config, ANTHROPIC_PROVIDER)
    at_a_gateway = settings(config, COMPATIBLE_PROVIDER)

    assert (at_the_vendor["anthropic_cache"], at_the_vendor["anthropic_cache_messages"]) == (
        True,
        False,
    )
    assert (at_a_gateway["anthropic_cache"], at_a_gateway["anthropic_cache_messages"]) == (
        False,
        True,
    )


@asyncio_test
async def test_thinking_is_streamed_as_reasoning_and_kept_out_of_the_answer() -> None:
    model = ScriptedModel([thinking("Hmm."), thinking(" Fair."), "An answer."])

    seen = await turn_of(engine(model))

    assert seen == [
        ReasoningDelta(text="Hmm."),
        ReasoningDelta(text=" Fair."),
        TextDelta(text="An answer."),
        done(seen),
    ]
    assert done(seen).text == "An answer."


@asyncio_test
async def test_a_model_that_says_nothing_is_asked_again_by_the_framework() -> None:
    """The loop's own retry: an answer with neither text nor a call in it is
    not an answer, and the framework sends the model a retry prompt before
    the turn fails. A second silence is the failure (``MAX_RETRIES``)."""
    model = ScriptedModel([""], ["Something, then."])

    seen = await turn_of(engine(model))

    check_backend_events(seen)
    assert done(seen).text == "Something, then."
    assert model.calls == 2
    assert [part.part_kind for part in model.seen[1][-1].parts] == ["retry-prompt"]

    silent = ScriptedModel([""], [""])
    with pytest.raises(UnexpectedModelBehavior):
        await turn_of(engine(silent))


@asyncio_test
async def test_a_model_that_does_not_stream_still_arrives_as_one_delta() -> None:
    # Pydantic AI hands the answer over in pieces whatever the provider did;
    # one piece is what a whole answer looks like, and it is still the answer.
    model = ScriptedModel(["All of it at once."])

    seen = await turn_of(engine(model))

    check_backend_events(seen)
    assert seen == [TextDelta(text="All of it at once."), done(seen)]
    assert done(seen).text == "All of it at once."


# --- the tools -------------------------------------------------------------


@asyncio_test
async def test_the_agents_servers_reach_the_framework_with_the_secrets_in_their_order() -> None:
    handed: list[tuple[Sequence[ToolServerConfig], ToolServerSecrets]] = []

    def record(servers: Sequence[ToolServerConfig], secrets: ToolServerSecrets) -> list[Any]:
        handed.append((servers, secrets))
        return []

    model = ScriptedModel(["Done."])
    agent = PydanticAIAgent(
        models(servers=(GITHUB, DOCS), agent=definition(tools=(DOCS.id, GITHUB.id))),
        keys(),
        SECRETS,
        model_for=lambda *_: model.model,  # noqa: ARG005
        toolsets_for=record,
    )

    await turn_of(agent, definition_=definition(tools=(DOCS.id, GITHUB.id)))

    ((servers, secrets),) = handed
    assert list(servers) == [DOCS, GITHUB]
    assert secrets is SECRETS
    assert model.bound == [[]]


@asyncio_test
async def test_the_tools_are_declared_to_the_model_as_the_framework_takes_them() -> None:
    model = ScriptedModel(["Done."])

    await turn_of(engine(model))

    assert model.bound == [[SEARCH]]


@asyncio_test
async def test_a_tool_that_fails_is_a_result_the_model_reads_not_a_failure_of_the_turn() -> None:
    """The framework's loop, not the platform's: a tool that failed is an
    error result, the model is asked again with it, and the turn goes on
    (``docs/specs/runs.md``, "Tools")."""
    model = ScriptedModel(calling("toolu_01", SEARCH, {"q": "boom"}), ["No such repository, then."])

    seen = await turn_of(engine(model))

    check_backend_events(seen)
    (result,) = [event for event in seen if isinstance(event, ToolResult)]
    assert (result.call_id, result.name, result.is_error) == ("toolu_01", SEARCH, True)
    assert "no such repo" in result.output
    assert done(seen).text == "No such repository, then."
    # The second call was made with the failed result in front of the model.
    assert [part.part_kind for part in model.seen[1][-1].parts] == ["tool-return"]


@asyncio_test
async def test_a_call_the_framework_sends_back_for_another_try_is_an_error_result_too() -> None:
    # A retry prompt: the framework wrote it, the model reads it, and the
    # transcript shows it under the call it answers.
    model = ScriptedModel(calling("toolu_01", SEARCH, {"q": "again"}), ["Once more."])

    seen = await turn_of(engine(model))

    check_backend_events(seen)
    (result,) = [event for event in seen if isinstance(event, ToolResult)]
    assert (result.call_id, result.name, result.is_error) == ("toolu_01", SEARCH, True)
    assert "try again" in result.output
    assert done(seen).text == "Once more."


@asyncio_test
async def test_a_result_longer_than_a_part_is_cut_to_one_for_the_transcript_only() -> None:
    long = "x" * (MAX_PART_CHARS + 10)

    def read(path: str) -> str:  # noqa: ARG001 - the model's argument
        """Read a file."""
        return long

    model = ScriptedModel(calling("toolu_01", "read", {"path": "a"}), ["Read."])
    agent = PydanticAIAgent(
        models(),
        keys(),
        model_for=lambda *_: model.model,  # noqa: ARG005
        toolsets_for=lambda *_: [FunctionToolset([read])],  # noqa: ARG005
    )

    seen = await turn_of(agent)

    (result,) = [event for event in seen if isinstance(event, ToolResult)]
    assert result.output == long[:MAX_PART_CHARS]
    # What the model was sent is the framework's, whole.
    (returned,) = model.seen[1][-1].parts
    assert isinstance(returned, ToolReturnPart)
    assert returned.content == long


@asyncio_test
async def test_a_turn_that_never_stops_calling_tools_is_bounded_by_the_framework() -> None:
    # More rounds than the bound allows, each asking once more.
    model = ScriptedModel(
        *(
            calling(f"toolu_{at:02}", SEARCH, {"q": "robinauts"})
            for at in range(2 * MAX_TOOL_ROUNDS)
        )
    )
    agent = engine(model)
    seen: list[Event] = []

    with pytest.raises(UsageLimitExceeded):
        async for event in agent.stream(definition(), PROMPT, model=MODEL, state=None):
            seen.append(event)

    check_backend_events(seen, cut_short=True)
    assert len([event for event in seen if isinstance(event, ToolCall)]) == MAX_TOOL_ROUNDS + 1
    assert (agent.held, model.open_streams) == (0, 0)


# --- the context -------------------------------------------------------------


def a_long_history(exchanges: int = 15, chars: int = 200) -> list[ModelMessage]:
    """A memory past any small window: that many exchanges of that many characters."""
    return [
        message
        for at in range(exchanges)
        for message in (
            question(f"{at}: " + "word " * (chars // 5)),
            answer("word " * (chars // 5)),
        )
    ]


@asyncio_test
async def test_a_history_past_the_window_is_trimmed_by_whole_exchanges_from_the_oldest() -> None:
    """Before the model call and on the memory handed back alike: the oldest
    questions and everything after each go, the latest stay whole."""
    model = ScriptedModel(["The answer."])
    history = a_long_history()

    seen = await turn_of(engine(model, context_window=1_000), state=write_state(history))

    check_backend_events(seen)
    sent = model.seen[0]
    assert 0 < len(sent) < len(history) + 1
    # What was sent begins at a question, and ends in this turn's.
    assert said(sent)[0][0] == "user-prompt"
    assert said(sent)[-1] == ("user-prompt", PROMPT)
    # And the memory is what the model saw plus its answer, kept to the
    # window once more now that the answer is in it.
    remembered = said(read_state(done(seen).state))
    assert remembered[-1] == ("text", "The answer.")
    assert remembered[0][0] == "user-prompt"
    assert (said(sent) + [("text", "The answer.")])[-len(remembered) :] == remembered


@asyncio_test
async def test_a_history_within_the_window_is_sent_as_it_is() -> None:
    model = ScriptedModel(["The answer."])
    history = a_long_history()

    seen = await turn_of(engine(model), state=write_state(history))

    assert len(model.seen[0]) == len(history) + 1
    assert len(read_state(done(seen).state)) == len(history) + 2


@asyncio_test
async def test_a_tool_round_is_never_parted_from_its_question_by_the_trimming() -> None:
    kept = within(100)
    round_ = [
        question("Look."),
        ModelResponse(parts=[ToolCallPart(tool_name=SEARCH, args={}, tool_call_id="toolu_01")]),
        ModelRequest(
            parts=[ToolReturnPart(tool_name=SEARCH, content="found 3", tool_call_id="toolu_01")]
        ),
        answer("Found."),
    ]

    trimmed = await kept([*a_long_history(exchanges=4), *round_, question("And?")])

    assert said(trimmed)[0] == ("user-prompt", "And?")
    trimmed = await kept([*a_long_history(exchanges=4), *round_])
    assert said(trimmed) == said(round_)


@asyncio_test
async def test_the_latest_exchange_stays_whatever_it_measures_and_one_exchange_is_kept() -> None:
    kept = within(1)

    assert said(await kept([question("A" * 1000)])) == [("user-prompt", "A" * 1000)]
    assert said(await kept([question("Old"), answer("Old."), question("B" * 1000)])) == [
        ("user-prompt", "B" * 1000)
    ]


def test_the_window_is_the_configured_one_then_the_frameworks_then_the_default() -> None:
    configured = ModelConfig(id=MODEL, provider=PROVIDER, name="c", context_window=5_000)
    known = ModelConfig(id=MODEL, provider=PROVIDER, name="claude-haiku-4-5")
    unknown = ModelConfig(id=MODEL, provider=PROVIDER, name="anthropic/claude-sonnet-5")

    assert context_window(ScriptedModel().model, configured) == 5_000
    assert context_window(chat_model(known, ANTHROPIC_PROVIDER, KEY), known) == 200_000
    assert context_window(ScriptedModel().model, unknown) == DEFAULT_CONTEXT_WINDOW


# --- the tool servers, as the framework's client connects to them ------------


def connection_of(built: object) -> tuple[MCPToolset, Any]:
    """The framework's toolset under the prefix, and the transport it would open."""
    assert isinstance(built, PrefixedToolset)
    inner = built.wrapped
    assert isinstance(inner, MCPToolset)
    return inner, inner.client.transport


def test_a_bearer_server_is_reached_at_its_endpoint_with_its_secret_under_its_id() -> None:
    built = mcp_toolset(GITHUB, SECRETS)

    inner, transport = connection_of(built)
    assert built.prefix == GITHUB.id
    assert inner.id == GITHUB.id
    assert transport.url == GITHUB.url
    assert transport.headers == {"Authorization": "Bearer s3cret"}
    assert inner.tool_error_behavior == "failed"
    # Both timeouts are the server's: the connection, and every read from it.
    assert inner.client._init_timeout == GITHUB.timeout_seconds
    assert inner.client._session_kwargs["read_timeout_seconds"] == timedelta(
        seconds=GITHUB.timeout_seconds
    )


def test_a_basic_server_sends_the_user_and_the_secret_as_the_scheme_says() -> None:
    server = ToolServerConfig(
        id="wiki",
        url="https://wiki.example.test/mcp",
        auth=ToolServerAuth.BASIC,
        user="robin",
        secret_env="WIKI_TOKEN",
    )
    pair = base64.b64encode(b"robin:s3cret").decode("ascii")

    _, transport = connection_of(mcp_toolset(server, ToolServerSecrets({"wiki": "s3cret"})))

    assert transport.headers == {"Authorization": f"Basic {pair}"}


def test_a_public_server_is_sent_no_credential() -> None:
    inner, transport = connection_of(mcp_toolset(DOCS, SECRETS))

    assert transport.headers == {}
    assert inner.client._init_timeout == 7.5


@asyncio_test
async def test_the_engine_holds_nothing_after_a_turn_that_ended() -> None:
    model = ScriptedModel(["Done."])
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

    def client_for(model: ModelConfig, *_: object) -> Any:
        built.append(model)
        return ScriptedModel(["Done."]).model

    agent = PydanticAIAgent(two_models(), keys(), model_for=client_for, toolsets_for=plain_tools)

    await turn_of(agent, model=OTHER_MODEL)

    assert [model.name for model in built] == ["claude-opus-5"]


@asyncio_test
async def test_a_model_the_configuration_does_not_have_fails_the_turn() -> None:
    # Where the stream is iterated, like any other failure of a turn, and
    # holding nothing afterwards.
    agent = engine(ScriptedModel(["Done."]))
    events = agent.stream(definition(), PROMPT, model="gpt-5-5", state=None)

    with pytest.raises(UnknownModelError):
        await anext(events)

    assert agent.held == 0


# --- the client the configuration describes ---------------------------------


def built_client(
    provider: ModelProviderConfig = ANTHROPIC_PROVIDER, **changes: Any
) -> anthropic.AsyncAnthropic:
    """The Anthropic client a model's configuration is turned into."""
    model = ModelConfig(id=MODEL, provider=provider.id, name="claude-sonnet-5", **changes)
    built = chat_model(model, provider, KEY)
    assert isinstance(built, AnthropicModel)
    assert built.model_name == "claude-sonnet-5"
    client = built.client
    assert isinstance(client, anthropic.AsyncAnthropic)
    return client


def test_anthropic_is_built_with_the_key_the_endpoint_and_no_retries() -> None:
    client = built_client()

    assert client.api_key == KEY
    assert str(client.base_url).rstrip("/") == ANTHROPIC_ENDPOINT
    assert client.max_retries == MAX_RETRIES


def test_the_key_is_not_in_what_the_keys_print() -> None:
    printed = repr(keys())

    assert KEY not in printed
    assert PROVIDER in printed


REDIRECTING_VARIABLES = {
    # Read by the SDK when no base URL is passed: one of them inherited from a
    # shell, a unit file or a container image would send every turn -- and the
    # key -- to whatever host it named.
    "ANTHROPIC_BASE_URL": "https://evil.example.test",
    # Read by the SDK when no credential is passed, and enough on their own to
    # spend somebody else's key.
    "ANTHROPIC_API_KEY": "sk-somebody-elses-key",
    "ANTHROPIC_AUTH_TOKEN": "somebody-elses-token",
    # The SDK's credential auto-discovery: a named profile on disk, and the
    # workload-identity federation chain. An explicit key switches all of it
    # off, endpoint included -- a profile can carry a base URL of its own.
    "ANTHROPIC_PROFILE": "somebody-elses-profile",
    "ANTHROPIC_IDENTITY_TOKEN": "somebody-elses-identity",
    "ANTHROPIC_FEDERATION_RULE_ID": "rule-1",
    "ANTHROPIC_ORGANIZATION_ID": "org-1",
    # A proxy variable only LangChain's client obeys, set here to show that
    # this one does not obey it either: an operator's proxy is HTTPS_PROXY.
    "ANTHROPIC_PROXY": "http://evil.example.test:3128",
}
"""An environment doing everything it can to redirect a turn and its key."""


def test_the_endpoint_and_the_key_come_from_the_configuration_and_never_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name, value in REDIRECTING_VARIABLES.items():
        monkeypatch.setenv(name, value)

    client = built_client()

    assert str(client.base_url).rstrip("/") == ANTHROPIC_ENDPOINT
    assert client.api_key == KEY
    assert client.auth_token is None


def test_an_anthropic_compatible_provider_is_reached_at_the_configured_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The kind that names a protocol: the address is the operator's, and it is
    # still the **configuration** that decides it and never the environment.
    for name, value in REDIRECTING_VARIABLES.items():
        monkeypatch.setenv(name, value)

    client = built_client(COMPATIBLE_PROVIDER)

    assert str(client.base_url).rstrip("/") == COMPATIBLE_ENDPOINT
    assert client.api_key == KEY
    assert client.auth_token is None
    assert client.max_retries == MAX_RETRIES


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
    assert PydanticAIAgent.kinds == {ProviderKind.ANTHROPIC, ProviderKind.ANTHROPIC_COMPATIBLE}


@pytest.mark.parametrize(
    "kind", sorted(frozenset(ProviderKind) - PydanticAIAgent.kinds), ids=lambda kind: kind.value
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
        f"model_providers.vendor: this build of the Pydantic AI engine cannot reach"
        f" a {kind.value} provider"
    ]


CUSTOM_HEADERS = "ANTHROPIC_CUSTOM_HEADERS"
"""The one client variable an argument cannot override: headers are merged."""


def test_the_key_header_is_the_configured_key_whatever_the_environment_injects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The SDK merges this variable into the headers the caller passed, so one
    # line of it would replace the key header outright and a turn would be
    # spent on somebody else's key. The caller's header wins, and the engine is
    # a caller.
    monkeypatch.setenv(CUSTOM_HEADERS, f"{ANTHROPIC_KEY_HEADER}: sk-somebody-elses-key")

    sent = _headers_of(built_client())

    assert [value for name, value in sent if name.lower() == ANTHROPIC_KEY_HEADER] == [KEY]


def test_building_the_engine_takes_the_header_variable_out_of_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The other half: an argument cannot say "and nothing else", so the
    # variable itself goes when the engine is built at start-up.
    monkeypatch.setenv(CUSTOM_HEADERS, "anthropic-beta: nobody-configured-this")

    engine(ScriptedModel(["Never run."]))

    assert all(name not in os.environ for name in CLIENT_VARIABLES_REMOVED)


def _headers_of(client: anthropic.AsyncAnthropic) -> list[tuple[str, str]]:
    """The headers the client would really send, built without sending one."""
    request = client._build_request(
        anthropic._models.FinalRequestOptions.construct(
            method="post", url="/v1/messages", json_data={}
        )
    )
    return list(request.headers.multi_items())


# --- nothing phones home ----------------------------------------------------


TRACING_VARIABLES = {
    # Logfire's own, which is what `logfire.configure()` reads.
    "LOGFIRE_TOKEN": "not-a-real-token",
    "LOGFIRE_SEND_TO_LOGFIRE": "true",
    # OpenTelemetry's, which is what an exporter would be built from.
    "OTEL_EXPORTER_OTLP_ENDPOINT": "http://evil.example.test:4318",
    "OTEL_TRACES_EXPORTER": "otlp",
    "OTEL_SDK_DISABLED": "false",
    "OTEL_PYTHON_TRACER_PROVIDER": "sdk_tracer_provider",
    # Pydantic AI's own, none of which switches instrumentation on; the banner
    # is the one thing they reach, and the engine turns it off in Python.
    "PYDANTIC_AI_GATEWAY_API_KEY": "not-a-real-key",
    "PYDANTIC_AI_GATEWAY_BASE_URL": "https://evil.example.test",
}
"""An environment doing everything it can to turn hosted tracing on."""


def _refuse(*args: Any, **kwargs: Any) -> Any:
    """Every way out of the process, closed. Reaching one fails the test."""
    raise AssertionError("the engine tried to open a connection")


def no_sockets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing in this process can resolve a name or connect to anything.

    ``socket.socket`` itself is deliberately **not** replaced: an event loop
    makes a socket pair for its own wake-up pipe, so a test that took the class
    away would fail before it had run anything. What is taken away is every way
    of reaching another machine -- a lookup and a connect -- which is the claim.
    """
    monkeypatch.setattr(socket, "getaddrinfo", _refuse)
    monkeypatch.setattr(socket, "create_connection", _refuse)
    monkeypatch.setattr(socket.socket, "connect", _refuse)


class _NeverBuilt:
    """Anything that would instrument a run. Building one fails the test."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise AssertionError("the engine asked for a tracer")


@pytest.fixture
def asking_to_trace(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Everything asking for tracing, every socket blocked, and all put back.

    ``BANNER_ENABLED`` is process-wide on purpose and is restored here: a test
    that left the package's switch where it found it is a test that says
    nothing about the next one, and one that left it turned off would hide a
    later engine that forgot to turn it off itself. So is
    ``Agent._instrument_default``, which is set to ``True`` to stand in for a
    process where somebody called ``logfire.instrument_pydantic_ai()``: that is
    the switch the engine's per-agent ``instrument = False`` has to beat. It is
    private to Pydantic AI and is reached as such, which is the honest cost of
    checking a process-wide promise.
    """
    was_banner = pydantic_ai.BANNER_ENABLED
    was_default = FrameworkAgent._instrument_default
    for name, value in TRACING_VARIABLES.items():
        monkeypatch.setenv(name, value)
    FrameworkAgent.instrument_all(True)
    # `InstrumentationSettings` is the one place Pydantic AI reaches for an
    # OpenTelemetry tracer provider, which is the one thing that would open an
    # exporter and send a conversation to a third party. A stand-in that
    # refuses to exist is what catches an engine that asked for one.
    monkeypatch.setattr(pydantic_ai.agent, "InstrumentationSettings", _NeverBuilt)
    no_sockets(monkeypatch)
    try:
        yield
    finally:
        pydantic_ai.BANNER_ENABLED = was_banner
        FrameworkAgent._instrument_default = was_default


@asyncio_test
async def test_a_whole_turn_instruments_nothing_however_loudly_everything_asks(
    asking_to_trace: None,
) -> None:
    """The whole claim, in one turn: no tracer, no exporter, no connection.

    Instrumentation is on process-wide, every variable a Logfire or an
    OpenTelemetry exporter reads is set, anything that would build
    instrumentation refuses to exist, and no name can be resolved and no
    connection opened. The turn runs to its end all the same.
    """
    model = ScriptedModel(["Quietly."])

    seen = await turn_of(engine(model))

    assert [event.text for event in seen if isinstance(event, TextDelta)] == ["Quietly."]
    assert done(seen).text == "Quietly."


@asyncio_test
async def test_an_agent_that_did_not_say_no_would_be_instrumented(
    asking_to_trace: None,
) -> None:
    """The other half of the claim: the test above would notice.

    A framework agent built the way this engine builds one, minus the one line
    that says ``instrument = False``, is instrumented by the process-wide
    switch -- and is caught by the stand-in. Without this, the test above would
    pass just as well against an engine that turned nothing off.
    """
    runner: FrameworkAgent[None, str] = FrameworkAgent(
        model=ScriptedModel(["Loudly."]).model, name=AGENT, instructions=SYSTEM_PROMPT
    )

    with pytest.raises(AssertionError, match="asked for a tracer"):
        async with runner.iter(message_history=[]):
            pass  # pragma: no cover -- building the run is what raises


def test_the_engine_silences_the_framework_s_banner_the_moment_it_is_built(
    asking_to_trace: None,
) -> None:
    # On its first run Pydantic AI writes an advertisement for its hosted
    # observability to standard error. A server's log is not the place for it.
    pydantic_ai.BANNER_ENABLED = True

    engine(ScriptedModel(["Never run."]))

    assert pydantic_ai.BANNER_ENABLED is False


def test_turning_tracing_off_takes_no_variable_out_of_the_environment(
    asking_to_trace: None,
) -> None:
    """There is none to take: Pydantic AI has no environment switch for tracing.

    The LangGraph adapter unsets two variables because langchain-core *raises*
    when they are set and tracing is off. Nothing here reads one, so nothing
    here edits a process's environment -- and the empty tuple is asserted so
    that a variable added to it later has to come with a reason.
    """
    engine(ScriptedModel(["Never run."]))

    assert TRACING_VARIABLES_REMOVED == ()
    assert all(name in os.environ for name in TRACING_VARIABLES)


def test_no_instrumented_model_class_has_been_asked_for_a_tracer_in_this_process() -> None:
    """Importing the engine, building one and running turns asks for no tracer.

    ``InstrumentationSettings`` is the only thing in Pydantic AI that reaches
    for an OpenTelemetry tracer provider, and it is built only when an agent is
    instrumented. Nothing in this module instruments one, so the default
    provider is still the API's own no-op proxy: an SDK was never loaded, and
    no exporter could have been.
    """
    from opentelemetry.trace import ProxyTracerProvider, get_tracer_provider

    assert isinstance(get_tracer_provider(), ProxyTracerProvider)


# --- and nothing is written down ---------------------------------------------


LEAK_SECRET = "the-conversation-nobody-should-log"
"""Stands in for a message and a system prompt in the subprocess below."""

LEAK_KEY = "sk-the-key-nobody-should-log"

REQUEST = """
import anthropic
from robinauts.adapters import ProviderKeys
from robinauts.adapters.agents.pydantic_ai import chat_model
from robinauts.domain import ModelConfig, ModelProviderConfig, ModelsConfig, ProviderKind

{built}

provider = ModelProviderConfig(id="anthropic", kind=ProviderKind.ANTHROPIC, api_key_env="K")
model = chat_model(ModelConfig(id="m", provider="anthropic", name="claude"), provider, {key!r})
# The SDK writes its "Request options" record here, on the way to the wire and
# before anything is sent: no network is needed to make it leak.
model.client._build_request(
    anthropic._models.FinalRequestOptions.construct(
        method="post",
        url="/v1/messages",
        json_data={{"system": {secret!r}, "messages": [{{"role": "user", "content": {secret!r}}}]}},
    )
)
"""
"""A whole request built in a fresh process, with ``ANTHROPIC_LOG`` set.

A subprocess because the leak happens at **import**: ``anthropic`` reads the
variable at the bottom of its own ``__init__`` and puts its logger at
``DEBUG`` for the life of the process, which a test inside an already-imported
process cannot undo and cannot honestly stage.
"""

BUILT = "PydanticAIAgent(ModelsConfig(), ProviderKeys({}))"
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

    The platform's logs never carry conversation content
    (``quiet_client_logging``). The SDK's own answer to that variable is to
    log every request's options -- ``json_data`` and all, which is the system
    prompt and every message -- and building the engine is what takes it back.
    """
    quiet = in_a_fresh_process(
        REQUEST.format(
            built=f"from robinauts.adapters.agents.pydantic_ai import PydanticAIAgent\n{BUILT}",
            key=LEAK_KEY,
            secret=LEAK_SECRET,
        )
    )

    assert LEAK_SECRET not in quiet
    assert "Request options" not in quiet


@pytest.mark.io
def test_the_same_process_without_the_engine_really_does_leak_the_conversation() -> None:
    """The other half: the test above would notice.

    Without the one line that builds the engine, the same request in the same
    environment puts the conversation on standard error. A test of a silence
    is worth nothing until something has been heard.
    """
    leaked = in_a_fresh_process(REQUEST.format(built="", key=LEAK_KEY, secret=LEAK_SECRET))

    assert LEAK_SECRET in leaked
    assert "Request options" in leaked
    # And what a leak does **not** carry, shown against a real one rather than
    # against a silence: the key is a header, and the record is the request's
    # options without them. "Never the key" is worth asserting only where a
    # key would have shown up if it were ever going to.
    assert LEAK_KEY not in leaked


def test_the_shared_fixture_knows_every_logger_this_engine_pins() -> None:
    """``tests/conftest.py`` restores these for the whole suite, and cannot import them.

    It names them itself, because it is loaded for every test and importing an
    adapter there would make the whole suite import an agent framework. This
    is what keeps the two lists from drifting apart.
    """
    assert tuple(VENDOR_LOGGERS) == QUIET_CLIENT_LOGGERS


def test_building_the_engine_holds_the_vendor_loggers_at_warning() -> None:
    # The in-process half of the same promise: whatever those loggers were at,
    # they are at WARNING or stricter once the engine exists, so no handler
    # anybody attaches is ever given a request.
    for name in QUIET_CLIENT_LOGGERS:
        logging.getLogger(name).setLevel(logging.DEBUG)

    engine(ScriptedModel(["Never run."]))

    assert [logging.getLogger(name).getEffectiveLevel() for name in QUIET_CLIENT_LOGGERS] == [
        QUIET_CLIENT_LEVEL
    ] * len(QUIET_CLIENT_LOGGERS)


def test_a_logger_an_operator_silenced_further_is_left_where_it_is() -> None:
    # Raising a level is the promise; lowering one would be this adapter
    # overruling somebody who meant it.
    logging.getLogger(QUIET_CLIENT_LOGGERS[0]).setLevel(logging.CRITICAL)

    engine(ScriptedModel(["Never run."]))

    assert logging.getLogger(QUIET_CLIENT_LOGGERS[0]).level == logging.CRITICAL


def test_building_the_engine_takes_the_logging_variable_out_of_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # So that a subprocess this deployment starts does not import the SDK and
    # turn its own logging on again. It cannot undo this process's import,
    # which is what the level above is for.
    monkeypatch.setenv("ANTHROPIC_LOG", "debug")

    engine(ScriptedModel(["Never run."]))

    assert "ANTHROPIC_LOG" in CLIENT_VARIABLES_REMOVED
    assert all(name not in os.environ for name in CLIENT_VARIABLES_REMOVED)


def test_logfire_is_not_installed_at_all() -> None:
    """The one part of "nothing phones home" that is the lock's and not the code's.

    ``pydantic-graph`` opens spans through ``logfire_api``, which is a **no-op
    shim** while the real package is absent -- and which replaces itself with
    the real ``logfire`` module the moment that package is importable, outside
    anything ``instrument = False`` reaches. So the guarantee that those spans
    go nowhere rests on ``logfire`` not being in the locked set, and that is
    asserted here rather than left as something somebody once checked. A build
    that added it would have to answer this test
    (``docs/specs/agents.md``, "Known findings").
    """
    assert importlib.util.find_spec("logfire") is None


def a_request_carrying(secret: str) -> None:
    """Build a whole request with that text in it, and send nothing.

    The same call the subprocess above makes, in this process: it is where the
    SDK writes its record, and it happens before anything reaches a socket.
    """
    built_client()._build_request(
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

    This is the case a pin on the *effective* level would miss entirely. With
    ``ANTHROPIC_LOG`` unset and the root at ``WARNING``, the vendor's loggers
    sit at ``NOTSET`` and are already effectively quiet -- so a pin that asked
    what they were effectively at would set nothing, and the moment an operator
    turned their own root logger up to ``DEBUG``, which is a thing an operator
    does, every message of every conversation would arrive on standard error
    from a switch that had nothing to do with the vendor.
    """
    engine(ScriptedModel(["Never run."]))

    with caplog.at_level(logging.DEBUG):
        a_request_carrying(LEAK_SECRET)

    assert LEAK_SECRET not in caplog.text
    assert "Request options" not in caplog.text


def test_the_same_root_logger_leaks_it_when_no_engine_pinned_anything(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The other half: a process where nothing pinned them really does leak.

    The loggers are put back to ``NOTSET``, which is where a fresh process has
    them, so what is staged is "the engine was never built" rather than "the
    engine was built and undone".
    """
    for name in QUIET_CLIENT_LOGGERS:
        logging.getLogger(name).setLevel(logging.NOTSET)

    with caplog.at_level(logging.DEBUG):
        a_request_carrying(LEAK_SECRET)

    assert LEAK_SECRET in caplog.text


def test_a_level_named_on_the_emitting_logger_itself_is_raised_too() -> None:
    """A ``dictConfig`` entry naming the child walks past a pin on the parent.

    ``anthropic._base_client`` is the module that emits the record, and a
    logger decides by its **own** level: a deployment whose logging
    configuration names it at ``DEBUG`` would be unaffected by anything set on
    ``anthropic``. It is found in that state at start-up and raised, which is
    the state this can answer for (``QUIET_CLIENT_LOGGERS``).
    """
    emitting = logging.getLogger("anthropic._base_client")
    emitting.setLevel(logging.DEBUG)

    engine(ScriptedModel(["Never run."]))

    assert emitting.level == QUIET_CLIENT_LEVEL
    assert "anthropic._base_client" in QUIET_CLIENT_LOGGERS
