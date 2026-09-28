# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The Pydantic AI engine: the contract suite, the mapping, and what it never does.

The engine is run for real -- its framework agent built, its graph iterated,
its stream consumed, its ``finally`` exercised -- over one of Pydantic AI's own
test models, scripted by the test. No network, no key: what is replaced is the
one seam the adapter has for it (``PydanticAIAgent(model_for=...)``), and
everything else is the code a deployment runs.

Four subjects, the same four the LangGraph engine's module has:

- the **contract** both engines are held to (``contracts/agents.py``), run here
  over the second real engine;
- the **mapping**: what the model is given, and what its stream events become.
  A turn of this engine holds one answer and always streams -- the turn stops
  at the end of the model's first answer, and the framework hands that answer
  over in pieces however the provider sent it -- which is what the two
  declarations on the contract subclass say;
- the **client**: that a model's configuration reaches Anthropic's client as
  the key, the endpoint, the retries and the headers it describes, asserted on
  the constructed object because constructing one reaches nothing;
- **nothing phones home**: with the environment asking loudly for tracing and
  every socket blocked, a whole turn runs, instruments nothing, and opens
  nothing.
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import os
import socket
import subprocess
import sys
from collections.abc import AsyncIterator, Iterator, Sequence
from typing import Any

import anthropic
import pydantic_ai
import pydantic_ai.agent
import pytest
from pydantic_ai import Agent as FrameworkAgent
from pydantic_ai.models import Model
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.function import AgentInfo, DeltaThinkingPart, DeltaToolCall, FunctionModel

from aio import asyncio_test
from conftest import VENDOR_LOGGERS
from contracts.agents import AgentContract, Ending, Script
from conversations import agent_definition, answer, question
from robinauts.adapters import ProviderKeys
from robinauts.adapters.agents.pydantic_ai import (
    ANTHROPIC_ENDPOINT,
    ANTHROPIC_KEY_HEADER,
    CLIENT_VARIABLES_REMOVED,
    DEFAULT_ANTHROPIC_OUTPUT_TOKENS,
    MAX_RETRIES,
    QUIET_CLIENT_LEVEL,
    QUIET_CLIENT_LOGGERS,
    TRACING_VARIABLES_REMOVED,
    PydanticAIAgent,
    chat_model,
    endpoint_of,
)
from robinauts.domain import (
    KINDS_WITH_BASE_URL,
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
    ToolDefinition,
    UnknownModelError,
    UnsupportedContentError,
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
    """A configuration with one provider, one model and one agent on Pydantic AI."""
    model = ModelConfig(id=MODEL, provider=PROVIDER, name="claude-sonnet-5", **changes)
    return ModelsConfig(
        providers={PROVIDER: ANTHROPIC_PROVIDER},
        models={MODEL: model},
        agents={AGENT: definition()},
    )


def definition() -> AgentDefinition:
    return agent_definition(
        id=AGENT, model=MODEL, engine=Engine.PYDANTIC_AI, system_prompt=SYSTEM_PROMPT
    )


def keys() -> ProviderKeys:
    return ProviderKeys({PROVIDER: KEY})


class ScriptedModel:
    """A model a test writes the stream of, and can stop or break.

    A real ``FunctionModel`` underneath -- ``model`` is what the engine is
    handed -- so the framework's agent, its graph, its streaming and its
    releasing are exercised as they would be against a provider, and so that a
    cancellation reaches an ``await`` the way it would reach a socket.
    ``open_streams`` is what "the engine released what it held" looks like from
    underneath: the ``finally`` of the generator below is reached only when the
    stream is closed.

    Items are yielded as Pydantic AI's stream functions yield them: a ``str``
    is text, and a mapping of ``DeltaThinkingPart`` or ``DeltaToolCall`` is
    thinking or a tool call.
    """

    def __init__(
        self,
        *items: Any,
        error: BaseException | None = None,
        gate: asyncio.Event | None = None,
    ) -> None:
        self.items = list(items)
        """What the stream yields, one item each."""
        self.error = error
        """Raised after the items, which is how a provider fails mid-answer."""
        self.gate = gate
        """Awaited after the items: a turn that hangs, for the cancellation test."""
        self.seen: list[list[Any]] = []
        """Every call's messages, exactly as the framework handed them over."""
        self.instructions: list[str | None] = []
        """The instructions of every call: where the system prompt arrives."""
        self.settings: list[Any] = []
        """The model settings of every call: the timeout and the ceiling."""
        self.open_streams = 0
        self.model: Model = FunctionModel(stream_function=self._stream, model_name="scripted")

    async def _stream(self, messages: list[Any], info: AgentInfo) -> AsyncIterator[Any]:
        self.seen.append(list(messages))
        self.instructions.append(info.instructions)
        self.settings.append(info.model_settings)
        self.open_streams += 1
        try:
            for item in self.items:
                yield item
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


def scripted(script: Script) -> ScriptedModel:
    """The model one turn of that script needs.

    A turn of this engine is one call to the model, so it is the script's first
    answer that is scripted; ``answers_per_turn`` below is what keeps the suite
    from asking for a second.
    """
    said = script.answers[0].text if script.answers else ""
    streams = not script.answers or script.answers[0].streamed
    # A stream function must yield at least one item, so an answer with nothing
    # in it is one empty piece of text -- which is also how a provider that
    # said nothing arrives.
    items = (in_pieces(said) if streams else [said]) if said else [""]
    return ScriptedModel(
        *items,
        error=RuntimeError("the provider said no") if script.ending is Ending.FAIL else None,
        gate=asyncio.Event() if script.ending is Ending.HANG else None,
    )


class TestPydanticAIAgent(AgentContract):
    """The real engine, held to everything the port promises."""

    answers_per_turn = 1
    """The turn stops at the end of the model's first answer, so it holds one."""

    can_answer_without_streaming = False
    """Pydantic AI hands the answer over in pieces even when the model sent one."""

    can_call_tools = False
    """Until this engine binds tools (two steps on), its model cannot be scripted to call one."""

    def new_agent(self, script: Script) -> Agent:
        self.model = scripted(script)
        return PydanticAIAgent(
            models(), keys(), model_for=lambda *_: self.model.model  # noqa: ARG005
        )

    def held(self, agent: Agent) -> int:
        assert isinstance(agent, PydanticAIAgent)
        # Both halves: the engine's own turn, and the model's stream inside it.
        # A turn that let go of the first and not the second would still be
        # holding a response open.
        return agent.held + self.model.open_streams

    def definition(self) -> AgentDefinition:
        return definition()


# --- what the model is given ------------------------------------------------


async def turn_of(
    agent: PydanticAIAgent,
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


def engine(model: ScriptedModel, **changes: Any) -> PydanticAIAgent:
    return PydanticAIAgent(
        models(**changes), keys(), model_for=lambda *_: model.model  # noqa: ARG005
    )


def said(messages: list[Any]) -> list[tuple[str, str | None]]:
    """A call's messages as (kind, text) pairs: what the model was really told."""
    return [
        (type(message).__name__, "".join(getattr(part, "content", "") for part in message.parts))
        for message in messages
    ]


@asyncio_test
async def test_the_system_prompt_is_an_instruction_and_not_one_of_the_messages() -> None:
    """``instructions``, which is the half of the framework that is not history.

    A ``system_prompt`` would become a ``SystemPromptPart`` inside
    ``message_history``; the platform's history holds messages and no system
    prompt, and the agent's is read afresh at every turn, which is exactly what
    ``instructions`` means.
    """
    model = ScriptedModel("Someone who plays fair.")
    asked = question("What is a robinaut?")

    await turn_of(engine(model), (asked,))

    (given,) = model.seen
    assert said(given) == [("ModelRequest", "What is a robinaut?")]
    assert model.instructions == [SYSTEM_PROMPT]


@asyncio_test
async def test_an_agent_with_no_system_prompt_sends_no_instruction() -> None:
    # An empty instruction is not the same as none.
    model = ScriptedModel("Hello.")

    await turn_of(
        engine(model),
        (question(),),
        definition_=agent_definition(
            id=AGENT, model=MODEL, engine=Engine.PYDANTIC_AI, system_prompt=""
        ),
    )

    assert model.instructions == [None]


@asyncio_test
async def test_the_history_keeps_its_roles_and_carries_no_reasoning() -> None:
    model = ScriptedModel("Again.")
    asked = question("What is a robinaut?")
    replied = answer(
        asked,
        parts=(ReasoningPart("thinking about it"), TextPart("Someone who plays fair.")),
    )
    again = question("And a robin?", parent=replied)

    await turn_of(engine(model), (asked, replied, again))

    (given,) = model.seen
    assert said(given) == [
        ("ModelRequest", "What is a robinaut?"),
        # The thinking of the previous turn is not sent back: this version
        # keeps none of it, and what is stored holds none either.
        ("ModelResponse", "Someone who plays fair."),
        ("ModelRequest", "And a robin?"),
    ]
    assert all(message.role is not Role.TOOL for message in (asked, replied, again))


@asyncio_test
async def test_the_timeout_and_the_ceiling_of_the_configuration_travel_with_the_request() -> None:
    model = ScriptedModel("Quickly.")

    await turn_of(engine(model, timeout_seconds=17.0, max_output_tokens=1234), (question(),))

    assert model.settings == [{"timeout": 17.0, "max_tokens": 1234}]


@asyncio_test
async def test_a_model_with_no_ceiling_configured_is_sent_the_engine_s_own() -> None:
    # Anthropic's API requires one on every request, so the engine sends one.
    model = ScriptedModel("Quickly.")

    await turn_of(engine(model), (question(),))

    assert model.settings[0]["max_tokens"] == DEFAULT_ANTHROPIC_OUTPUT_TOKENS


# --- what the model's stream becomes ----------------------------------------


@asyncio_test
async def test_thinking_is_streamed_as_reasoning_and_kept_out_of_the_answer() -> None:
    model = ScriptedModel(
        {0: DeltaThinkingPart(content="let me think")},
        "Someone who ",
        "plays fair.",
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
    # A model that streamed a piece with no text in it. A message has content,
    # so "the agent answered with nothing" is recorded as one empty part rather
    # than as a turn that produced no answer -- and the framework is never let
    # as far as the retry it would otherwise send for an empty answer.
    seen = await turn_of(engine(ScriptedModel("")), (question(),))

    assert seen == [AnswerStarted(), AnswerCompleted(parts=(TextPart(""),))]


@asyncio_test
async def test_a_character_split_across_two_deltas_is_put_back_together() -> None:
    # A provider splits where it likes; what is stored has to be storable.
    whole = "\N{ROCKET}"
    above = ord(whole) - 0x10000
    high, low = chr(0xD800 + (above >> 10)), chr(0xDC00 + (above & 0x3FF))

    seen = await turn_of(engine(ScriptedModel(high, low)), (question(),))

    assert seen[-1] == AnswerCompleted(parts=(TextPart(whole),))


@asyncio_test
async def test_a_model_that_does_not_stream_still_arrives_as_one_delta() -> None:
    """The "not streamed" shape, as this engine really produces it.

    The contract suite says an engine need not stream, and declares that this
    one always does (``can_answer_without_streaming = False``); what that means
    in practice is this. The model hands the whole answer over at once, the
    framework opens a text part with it, and the turn is announced, one delta,
    completed -- with exactly what was streamed, which is the promise that
    still holds.
    """
    whole = "All of it at once."

    seen = await turn_of(engine(ScriptedModel(whole)), (question(),))

    assert [type(event) for event in seen] == [AnswerStarted, AnswerTextDelta, AnswerCompleted]
    assert [event.text for event in seen if isinstance(event, AnswerTextDelta)] == [whole]
    assert seen[-1] == AnswerCompleted(parts=(TextPart(whole),))


@asyncio_test
async def test_one_turn_is_one_call_to_the_model() -> None:
    """The turn ends with the model's first answer, whatever the graph would do.

    Pydantic AI's graph answers an empty response, or a call to a tool that is
    not there, by sending the model a **retry prompt** and asking again -- a
    second answer in one turn that nobody asked for and the operator pays for.
    Retrying is sending the message again (``docs/specs/runs.md``), so the turn
    is left after the first answer and the model is called once.
    """
    model = ScriptedModel("")

    await turn_of(engine(model), (question(),))

    assert len(model.seen) == 1


@asyncio_test
async def test_a_message_of_the_tool_role_is_never_sent_to_a_model() -> None:
    """The second of two refusals, and the one the engine owes.

    ``domain.Message`` already refuses the ``tool`` role, so one cannot be
    built or stored and the message below has to be forced into existence. The
    engine checks anyway: it is the line that turns a history into what a model
    is told, and the failure it would otherwise have is silent -- a tool result
    sent as though the agent had said it.
    """
    model = ScriptedModel("Never asked.")
    from_a_tool = question("the weather is fine")
    object.__setattr__(from_a_tool, "role", Role.TOOL)

    with pytest.raises(UnsupportedContentError) as raised:
        await turn_of(engine(model), (from_a_tool,))

    assert "tool" in str(raised.value)
    assert model.seen == []


@asyncio_test
async def test_a_model_that_asks_to_use_a_tool_fails_the_turn() -> None:
    # This version has no tools: every part after the call would belong to an
    # answer that cannot be produced, so finishing the turn with whatever text
    # came with it would store half an answer that looks whole. An engine
    # reports that by raising.
    model = ScriptedModel({0: DeltaToolCall(name="search", json_args="{}", tool_call_id="t1")})
    agent = engine(model)

    with pytest.raises(UnsupportedContentError) as raised:
        await turn_of(agent, (question(),))

    assert "tool" in str(raised.value)
    assert (agent.held, model.open_streams) == (0, 0)
    # And it raised rather than letting the framework send its retry prompt.
    assert len(model.seen) == 1


@asyncio_test
async def test_a_turn_handed_tools_is_refused_until_the_engine_binds_them() -> None:
    # Binding the definitions to the model is the next step's; a turn run
    # without them would answer a question the model was meant to use a tool
    # for, so it is refused before the model is reached.
    model = ScriptedModel("Never asked.")
    search = ToolDefinition(name="github__search", description="Search.", input_schema={})

    with pytest.raises(UnsupportedContentError) as raised:
        await turn_of(engine(model), (question(),), tools=(search,))

    assert "bind tools" in str(raised.value)
    assert model.seen == []


@asyncio_test
async def test_the_whole_path_reaches_the_model_and_ends_in_the_question() -> None:
    """This engine's context policy, for now: everything (ADR 0004). What is
    kept above the port is that the question being answered is whole in what
    the model sees; it is checked here, where the model can be asked."""
    model = ScriptedModel("Three.")
    first = question("q" * 5_000, seconds=0)
    said = answer(first, "a" * 5_000, seconds=1)
    second = question("r" * 5_000, parent=said, seconds=2)
    replied = answer(second, "b" * 5_000, seconds=3)
    third = question("Are you sure?", parent=replied, seconds=4)

    await turn_of(engine(model), (first, said, second, replied, third))

    (heard,) = model.seen
    assert [type(message).__name__ for message in heard] == [
        "ModelRequest",
        "ModelResponse",
        "ModelRequest",
        "ModelResponse",
        "ModelRequest",
    ]
    assert [
        "".join(getattr(part, "content", "") for part in message.parts) for message in heard
    ] == [first.text, said.text, second.text, replied.text, "Are you sure?"]


@asyncio_test
async def test_the_engine_holds_nothing_after_a_turn_that_ended() -> None:
    model = ScriptedModel("Done.")
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

    def client_for(model: ModelConfig, *_: object) -> Any:
        built.append(model)
        return ScriptedModel("Done.").model

    agent = PydanticAIAgent(two_models(), keys(), model_for=client_for)

    await turn_of(agent, (question(),), model=OTHER_MODEL)

    assert [model.name for model in built] == ["claude-opus-5"]


@asyncio_test
async def test_a_model_the_configuration_does_not_have_fails_the_turn() -> None:
    # Where the stream is iterated, like any other failure of a turn, and
    # holding nothing afterwards.
    agent = engine(ScriptedModel("Done."))
    events = agent.run_turn(definition(), (question(),), (), model="gpt-5-5")

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

    engine(ScriptedModel("Never run."))

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
    model = ScriptedModel("Quietly.")

    seen = await turn_of(engine(model), (question(),))

    assert [event.text for event in seen if isinstance(event, AnswerTextDelta)] == ["Quietly."]
    assert seen[-1] == AnswerCompleted(parts=(TextPart("Quietly."),))


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
        model=ScriptedModel("Loudly.").model, name=AGENT, instructions=SYSTEM_PROMPT
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

    engine(ScriptedModel("Never run."))

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
    engine(ScriptedModel("Never run."))

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

    engine(ScriptedModel("Never run."))

    assert [logging.getLogger(name).getEffectiveLevel() for name in QUIET_CLIENT_LOGGERS] == [
        QUIET_CLIENT_LEVEL
    ] * len(QUIET_CLIENT_LOGGERS)


def test_a_logger_an_operator_silenced_further_is_left_where_it_is() -> None:
    # Raising a level is the promise; lowering one would be this adapter
    # overruling somebody who meant it.
    logging.getLogger(QUIET_CLIENT_LOGGERS[0]).setLevel(logging.CRITICAL)

    engine(ScriptedModel("Never run."))

    assert logging.getLogger(QUIET_CLIENT_LOGGERS[0]).level == logging.CRITICAL


def test_building_the_engine_takes_the_logging_variable_out_of_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # So that a subprocess this deployment starts does not import the SDK and
    # turn its own logging on again. It cannot undo this process's import,
    # which is what the level above is for.
    monkeypatch.setenv("ANTHROPIC_LOG", "debug")

    engine(ScriptedModel("Never run."))

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
    engine(ScriptedModel("Never run."))

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

    engine(ScriptedModel("Never run."))

    assert emitting.level == QUIET_CLIENT_LEVEL
    assert "anthropic._base_client" in QUIET_CLIENT_LOGGERS
