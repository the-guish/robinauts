# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""One turn on Pydantic AI: an agent built per turn, streamed, translated.

The second implementation of the ``Agent`` port (``robinauts.ports.agents``),
and the only module in the platform that may name Pydantic AI
(``docs/layout.md``, enforced by the import contracts in
``backend/pyproject.toml``). Everything about the framework stops here: what
crosses the port is the platform's own history in and the platform's own
events out, and the **discard test** is that five places name this
sub-package and deleting it and its dependencies breaks those and nothing
else: its import in ``robinauts.app`` and its one entry in ``ENGINES``; the
contract exceptions in ``backend/pyproject.toml`` that name the sub-package;
this sub-package's own tests; and the **shared swap fixtures** under
``tests/`` (``tests/engines.py``, ``tests/unit/test_engine_swap.py`` and the
configuration swap in ``tests/integration/test_create_app.py``), which exist
to name both engines at once and cannot be written without both. The
composition tests (``tests/unit/test_app_composition.py``) fail too and name
no adapter: they say that *both* engines are wired, which is a claim about the
table and not about either sub-package (``docs/layout.md``).

**Stateless per turn** (ADR 0002). The framework's agent is built for the turn
and thrown away, and the whole of the conversation goes in as
``message_history``: nothing is remembered between calls, which is what lets
the next turn of the same conversation run on the other engine.

**The system prompt is ``instructions=``, not ``system_prompt=``.** The two
differ in exactly the way that matters here: a ``system_prompt`` becomes a
``SystemPromptPart`` *in the message history*, carried along with it and
replayed, while ``instructions`` are taken from the agent's definition at
every run and never enter the history. The platform's history holds messages
and no system prompt (``docs/specs/conversations.md``), and editing an agent
must take effect at the next turn of its existing conversations
(``docs/specs/agents.md``) -- both of which are what ``instructions`` means.

**One model call is the whole of a turn.** The graph is iterated until its
first model request node has been streamed, and then left. What that keeps out
is the framework's own retrying: an answer with no text in it, or a model
asking for a tool nobody declared, makes Pydantic AI send a *retry prompt* and
ask the model again -- a second answer in one turn, charged to the operator,
that nobody asked for. Retrying is sending the message again
(``docs/specs/runs.md``), so the turn ends where the model's first answer ends.

**The mapping**, which is the whole of the translation:

- the node's stream yields ``PartStartEvent``s (a part, with the first of its
  content already in it) and ``PartDeltaEvent``s (more of one). Text becomes
  ``AnswerTextDelta``, thinking becomes ``AnswerReasoningDelta``, and the
  first of either opens the answer (``AnswerStarted``);
- ``PartEndEvent`` carries the **whole** part again and is passed over, as is
  every other event of the stream: repeating it would store the answer twice;
- **what was streamed is what is kept** (``docs/specs/agents.md``): the answer
  completes with exactly the text deltas, joined. The framework's own final
  message is never read -- the turn does not run far enough to have one;
- **reasoning is never in the completed parts.** ``domain.kept_parts`` would
  drop a ``ReasoningPart`` anyway; putting the model's thinking in the
  answer's *text* is the mistake that would survive that, so the text part is
  built from the text deltas alone.

**Nothing phones home** (``docs/specs/core.md``). Pydantic AI's instrumentation
is turned off explicitly on every agent it builds (``force_tracing_off``,
``_runner``), so no tracer, no exporter and no Logfire client is ever made
whatever the environment says; and a vendor's client is built from the
configuration rather than from the environment -- the endpoint, the key and
the key's header (``ANTHROPIC_ENDPOINT``, ``clear_client_overrides``).

**And nothing is written down either.** The platform's logs never carry
conversation content: the vendor SDK's loggers that write request bodies --
which on ``ANTHROPIC_LOG``, and on any root logger turned up afterwards, put
every request's messages and system prompt on standard error -- are pinned at
``WARNING`` when the engine is built (``quiet_client_logging``).

**Failure and cancellation** are the port's. Whatever the provider raises
travels out of the generator as it is; ``CancelledError`` is never swallowed;
and the ``finally`` leaves the framework's two context managers, which is what
lets go of the model's stream, the HTTP response underneath it and the
connection.

**What the ``finally`` does not do is close the vendor client.** A client is
built per turn (``chat_model``) and is released to the garbage collector with
the rest of the turn; its connection pool is closed by the HTTP client's own
finaliser and not by this adapter. Both engines are the same in this, and a
shared client held for the life of the process -- and closed by the lifespan,
as the identity provider's is -- is a change to both adapters and to the
composition root, which is a step of its own
(``docs/working-notes/poc-progress.md``).
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncGenerator, Callable, Sequence

import pydantic_ai
from anthropic import AsyncAnthropic
from pydantic_ai import Agent as FrameworkAgent
from pydantic_ai import ModelRequest, ModelResponse, ModelSettings
from pydantic_ai.messages import (
    BaseToolCallPart,
    ModelMessage,
    ModelResponseStreamEvent,
    PartDeltaEvent,
    PartStartEvent,
    TextPartDelta,
    ThinkingPart,
    ThinkingPartDelta,
    ToolCallPartDelta,
    UserPromptPart,
)
from pydantic_ai.messages import TextPart as FrameworkTextPart
from pydantic_ai.models import Model
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.providers.anthropic import AnthropicProvider

from robinauts.adapters.config_file import ProviderKeys
from robinauts.domain import (
    AgentDefinition,
    AnswerCompleted,
    AnswerReasoningDelta,
    AnswerStarted,
    AnswerTextDelta,
    ConfigError,
    EngineEvent,
    Message,
    ModelConfig,
    ModelProviderConfig,
    ModelsConfig,
    ProviderKind,
    Role,
    TextPart,
    ToolDefinition,
    UnsupportedContentError,
    clean_text,
    text_parts,
)
from robinauts.ports import Agent

ANTHROPIC_ENDPOINT = "https://api.anthropic.com"
"""Where Anthropic is, said here rather than left to the client to decide.

**A vendor's endpoint is not a thing to take from the environment.** The
Anthropic SDK falls back to ``ANTHROPIC_BASE_URL`` when no base URL is passed,
so one variable inherited from a shell, a unit file or a container image would
send every turn -- and the operator's key with it -- to any host that variable
named. The operator says where a provider is in the configuration
(``base_url``, and only for a kind that names a protocol rather than a vendor),
or it is this constant; there is no third answer, and no way for the
environment to be one (``docs/specs/operations.md``). ``endpoint_of`` is where
the choice between the two is made, and it is the whole of it.

Spelt out again here rather than imported from the LangGraph adapter: the two
adapters do not import each other (``docs/layout.md``), and deleting either
must leave the other whole.
"""

TRACING_VARIABLES_REMOVED: tuple[str, ...] = ()
"""What ``force_tracing_off`` takes out of the environment: nothing.

Deliberately empty, and named all the same so that the claim is written down
rather than merely true today. Pydantic AI has **no environment switch for
instrumentation**: it traces when, and only when, an agent's ``instrument``
says so -- set by ``logfire.instrument_pydantic_ai()``, by
``Agent.instrument_all()`` or per agent -- and ``_runner`` sets it to ``False``
on every agent this engine builds, which beats both of the others. Nothing is
read from ``LOGFIRE_TOKEN``, ``LOGFIRE_SEND_TO_LOGFIRE``,
``OTEL_EXPORTER_OTLP_ENDPOINT``, ``OTEL_TRACES_EXPORTER`` or any
``PYDANTIC_AI_*`` variable on the path a turn takes: an ``OTel`` tracer
provider is asked for only while *building* instrumentation settings, which
never happens. So there is nothing here that an argument cannot say, and
editing a process's environment for the sake of saying it again would be an
adapter reaching further than it needs to (compare the LangGraph adapter,
where a variable really is load-bearing).
"""

CLIENT_VARIABLES_REMOVED = ("ANTHROPIC_CUSTOM_HEADERS", "ANTHROPIC_LOG")
"""The variables ``clear_client_overrides`` takes out of the environment.

The two the client reads that **no argument can override**; the endpoint and
the key are arguments, and an argument wins.

``ANTHROPIC_CUSTOM_HEADERS`` because the SDK *merges* what it holds into
whatever the caller passed, so one line of it replaces the ``x-api-key``
header outright and a turn is spent on somebody else's key, or adds headers
nobody configured. An argument cannot say "and nothing else", so the variable
goes.

``ANTHROPIC_LOG`` because it is read at **import**, before any argument
exists: ``anthropic`` calls its own ``setup_logging()`` at the bottom of its
``__init__``, and ``debug`` or ``info`` there puts the ``anthropic`` and
``httpx2`` loggers at that level for the whole process. What the first of them
then writes is every request's options, ``json_data`` included -- which is the
system prompt and every message of the conversation, in a log. Removing the
variable is only half the answer, since by construction time the import has
already happened: ``quiet_client_logging`` is the other half, and it is the
half that works.

Removed once, at construction, and never per turn: a process-wide edit made
while turns are running would be one turn changing another's environment.
"""

QUIET_CLIENT_LOGGERS = ("anthropic", "anthropic._base_client", "httpx2", "httpcore2")
"""The loggers ``quiet_client_logging`` pins, and why these four.

``anthropic`` is the SDK's own, and ``anthropic._base_client`` is the module
that **emits** the record carrying a request's ``json_data`` -- the system
prompt and every message. The child is named as well as the parent because a
level set on a child is what a logger decides by: an ``anthropic._base_client``
entry in somebody's ``dictConfig`` would walk straight past a pin on
``anthropic``. ``httpx2`` is the HTTP client the SDK is built on -- a fork of
its own, so this is **not** the ``httpx`` the sign-in adapter uses and pinning
it silences nothing of ours -- and ``httpcore2`` is the connection layer under
that, which logs request lines and headers.

What this list is, and is not: the vendor loggers that write request bodies.
A logger an operator names at ``DEBUG`` in their own logging configuration
**after** start-up is their deliberate act on their own machine, and nothing
here fights it; what is answered is the state a process is *found* in.
"""

QUIET_CLIENT_LEVEL = logging.WARNING
"""The level those loggers are held at or above: no request is ever a record.

**The platform's logs never carry conversation content.** A message is content
of a conversation and belongs in the database (``docs/specs/conversations.md``);
a key is the operator's and is never logged (``docs/specs/agents.md``). Both
of those are in a vendor SDK's ``DEBUG`` records, so the honest guarantee is
that the records are never emitted -- not that nobody has attached a handler
that would catch them.
"""

ANTHROPIC_KEY_HEADER = "x-api-key"
"""The header Anthropic's key travels in, pinned by ``chat_model``.

Belt as well as braces, and per client rather than process-wide: a header the
caller passes wins over anything merged in from the environment, so the key
that is sent is the configured one whether or not the variable above was there
to be removed.
"""

MAX_RETRIES = 0
"""How many times a failed model call is retried by the client: not at all.

The client's own retries are invisible to everything above -- they would
lengthen a turn silently and could send the same prompt twice after a timeout
the provider had already accepted. A turn is bounded by the application
(``robinauts.application.Turns``), a failure is reported by raising, and
retrying is sending the message again (``docs/specs/runs.md``).
"""

DEFAULT_ANTHROPIC_OUTPUT_TOKENS = 8192
"""What Anthropic is asked for when the model's configuration says nothing.

Its API requires a ceiling on every request, so an engine that sent none would
not work at all; the number is the client's business rather than the
platform's, which is why ``ModelConfig.max_output_tokens`` defaults to "leave
it to the engine" and this is where "the engine" answers.
"""


ChatModelFactory = Callable[[ModelConfig, ModelProviderConfig, str], Model]
"""How a model is built: the model, its provider, and the provider's key.

Injectable so that the tests run the engine over one of Pydantic AI's own test
models -- the graph, the streaming, the mapping and the releasing are then
exercised for real with no network and no key (``docs/layout.md``, "Testing
strategy").
"""


def force_tracing_off() -> None:
    """Turn Pydantic AI's hosted observability off, whatever else is running.

    **Nothing phones home** (``docs/specs/core.md``, ``docs/specs/agents.md``).
    Pydantic AI instruments a run only when instrumentation settings resolve to
    something, which happens when ``logfire.configure()`` plus
    ``logfire.instrument_pydantic_ai()`` has set the process-wide default
    (``Agent.instrument_all()``), when an agent's own ``instrument`` asks for
    it, or when the model handed in is already an ``InstrumentedModel``. Only
    then is an OpenTelemetry tracer provider asked for, which is the only thing
    that would open an exporter and send a conversation to a third party.

    Two of those three are answered where an agent is built: ``_runner`` sets
    ``instrument = False`` on every agent this engine makes, which **beats**
    the process-wide default, and the model comes from this adapter's own
    factory and is never an instrumented one. There is deliberately no
    environment variable to unset, because there is none to read
    (``TRACING_VARIABLES_REMOVED``).

    What is left, and what this function is for, is the **banner**: on its
    first run in a process Pydantic AI writes an advertisement for its hosted
    observability to standard error, which has no business in a server's log.
    ``BANNER_ENABLED`` is the package's own switch for it, and is set here,
    once, when the engine is built -- alongside the environment edit the
    LangGraph adapter makes for the same reason, and for the same kind of
    reason: it is the layer that may.
    """
    pydantic_ai.BANNER_ENABLED = False
    for name in TRACING_VARIABLES_REMOVED:  # pragma: no cover -- none, and said why
        os.environ.pop(name, None)


def clear_client_overrides() -> None:
    """Take out of the environment what no argument can override.

    The other half of "a vendor's client is built from the configuration and
    not from the environment" (``ANTHROPIC_ENDPOINT``). An endpoint and a key
    are arguments, and an argument wins; **headers are merged**, so the only
    way to say "and nothing else" is for the variable not to be there
    (``CLIENT_VARIABLES_REMOVED``).

    An adapter may touch the environment -- it is the layer that may -- and
    this does it once, when the engine is built at start-up, so that no turn
    ever edits the environment another turn is reading. ``ANTHROPIC_LOG``
    goes with them so that a subprocess this deployment starts does not import
    the SDK and turn its own logging on again; what protects **this** process,
    where the import has already happened, is ``quiet_client_logging``.
    """
    for name in CLIENT_VARIABLES_REMOVED:
        os.environ.pop(name, None)


def quiet_client_logging() -> None:
    """Hold the vendor SDK's loggers at ``WARNING``: no request is ever a record.

    **The platform's logs never carry conversation content, and never a key.**
    The Anthropic SDK reads ``ANTHROPIC_LOG`` at *import* -- before anything
    here exists -- and on ``debug`` puts its own logger and its HTTP client's
    at ``DEBUG`` and calls ``logging.basicConfig()``, which attaches a handler
    to the root logger if nothing else has. What the SDK's logger then writes
    for every call is "Request options: ..." with the request's ``json_data``
    in it: the agent's system prompt and every message of the conversation, on
    standard error.

    Removing the variable cannot undo that, because the import is what read it
    (``CLIENT_VARIABLES_REMOVED``). What does undo it is this: the vendor's
    loggers (``QUIET_CLIENT_LOGGERS``) are pinned at ``WARNING``
    (``QUIET_CLIENT_LEVEL``) when the engine is built, so the records are never
    emitted and it does not matter who has attached a handler -- which is the
    only form of the promise this adapter can keep, since the platform does not
    own every handler in the process and a deployment may add its own.

    **Each logger's own level is what is decided on, not its effective one.**
    An ordinary deployment has the variable unset and the root at ``WARNING``,
    so these loggers sit at ``NOTSET`` and their *effective* level is already
    ``WARNING`` -- and a pin that looked at that would do nothing at all,
    leaving them inheriting whatever the root becomes later. An operator who
    then turns their root logger up to ``DEBUG``, which is a thing an operator
    does, would get every message of every conversation on standard error from
    a switch that had nothing to do with the vendor. So ``NOTSET`` is treated
    as "not pinned yet" and set, and the promise holds whatever the root is
    moved to afterwards.

    A level already **stricter** than ``WARNING`` is left where it is: an
    operator who silenced the SDK altogether meant it.
    """
    for name in QUIET_CLIENT_LOGGERS:
        logger = logging.getLogger(name)
        # `NOTSET` is spelt out rather than left to be the zero it is: it means
        # "inherit", which is the case this exists for, and a reader should not
        # have to know its value to see that it is covered.
        if logger.level == logging.NOTSET or logger.level < QUIET_CLIENT_LEVEL:
            logger.setLevel(QUIET_CLIENT_LEVEL)


def endpoint_of(provider: ModelProviderConfig) -> str:
    """Where that provider is: the address the operator gave, or Anthropic's own.

    The two kinds this engine reaches are a **vendor** and a **protocol**
    (``PydanticAIAgent.kinds``). ``anthropic`` is Anthropic, at
    ``ANTHROPIC_ENDPOINT`` and nowhere else, and it has no ``base_url`` to
    offer. ``anthropic-compatible`` is an endpoint the operator names that
    speaks the same Messages API -- OpenRouter's is one -- so the address is
    theirs, checked where every configured endpoint is
    (``domain.is_endpoint_url``: https, or http on the loopback interface, no
    query, no fragment and no credential written into it).

    **A base URL is a prefix the client appends the protocol's own path to**,
    so an operator reaching OpenRouter writes ``https://openrouter.ai/api``
    and the request goes to ``https://openrouter.ai/api/v1/messages``
    (``docs/specs/agents.md``).

    A provider of any other kind should not arrive here: the configuration was
    held to ``PydanticAIAgent.kinds`` at start-up. One that does -- a kind added to
    that set without a branch here, a caller that built a definition by hand --
    gets a refusal naming it, and never a turn sent to whichever endpoint
    happened to be nearest. The tests ask it of every kind the engine does not
    offer, so this is a branch that is exercised rather than merely written.

    Spelt out again here rather than imported from the LangGraph adapter: the
    two adapters do not import each other (``docs/layout.md``), and deleting
    either must leave the other whole.
    """
    if provider.kind is ProviderKind.ANTHROPIC:
        return ANTHROPIC_ENDPOINT
    if provider.kind is ProviderKind.ANTHROPIC_COMPATIBLE and provider.base_url:
        return provider.base_url
    raise ConfigError(
        [
            f"model_providers.{provider.id}: this build of the Pydantic AI engine cannot"
            f" reach a {provider.kind.value} provider"
        ]
    )


def chat_model(model: ModelConfig, provider: ModelProviderConfig, key: str) -> Model:
    """The model that model's configuration describes.

    The key is passed in and not read here: what may touch the environment is
    ``robinauts.adapters.config_file``, and what reaches this is one key for
    one call (``docs/specs/agents.md``).

    **The vendor's client is built here rather than by the provider**, because
    two of the things that must be pinned are the client's and not the
    provider's: ``max_retries`` (``MAX_RETRIES``) and the headers
    (``ANTHROPIC_KEY_HEADER``). ``AnthropicProvider(anthropic_client=...)``
    takes the finished client, so nothing is left for it to decide.

    **Everything the client would otherwise take from the environment is
    passed.** The key, so that ``ANTHROPIC_API_KEY`` and ``ANTHROPIC_AUTH_TOKEN``
    are never consulted and the key a turn spends is the one the operator
    configured for that provider -- an explicit credential also switches the
    SDK's auto-discovery off outright, so a profile on disk, a federation
    token or an ``ANTHROPIC_PROFILE`` cannot supply one either; and the
    endpoint (``endpoint_of``), so that ``ANTHROPIC_BASE_URL`` cannot redirect
    a turn and a key to another host, nor a profile quietly fill one in -- the
    configuration is the only thing that decides where a turn goes. There is
    no vendor-specific proxy variable in this SDK to pin off: an
    operator's proxy is ``HTTPS_PROXY``, which is theirs and is how every other
    outbound call of this process is proxied.

    Headers are the one thing an argument cannot settle on its own, because
    the SDK **merges** ``ANTHROPIC_CUSTOM_HEADERS`` into what the caller
    passed: the key is therefore pinned as a header too
    (``ANTHROPIC_KEY_HEADER``), where a caller's value wins, and the variable
    itself is taken out of the environment when the engine is built
    (``clear_client_overrides``).

    The timeout and the output ceiling are **not** here: they travel with the
    request, as ``ModelSettings`` (``_runner``), which is where the model's
    configuration reaches a turn.
    """
    client = AsyncAnthropic(
        api_key=key,
        base_url=endpoint_of(provider),
        max_retries=MAX_RETRIES,
        default_headers={ANTHROPIC_KEY_HEADER: key},
    )
    return AnthropicModel(model.name, provider=AnthropicProvider(anthropic_client=client))


class PydanticAIAgent(Agent):
    """The Pydantic AI engine: one turn, one agent, built and thrown away."""

    kinds: frozenset[ProviderKind] = frozenset(
        {ProviderKind.ANTHROPIC, ProviderKind.ANTHROPIC_COMPATIBLE}
    )
    """The provider kinds this build of the engine has a client for.

    One client, two kinds: ``AsyncAnthropic`` reaches Anthropic itself and any
    endpoint that speaks Anthropic's Messages API at an address the operator
    gives (``endpoint_of``). **That is how OpenRouter is reached in this
    build**: it serves the Messages API and takes the key in the same
    ``x-api-key`` header, so nothing about the client changes but where it
    sends the request.

    What is still not here is ``openai`` and ``openai-compatible``, which are
    reached through ``pydantic-ai-slim[openai]``, which requires ``tiktoken``
    and through it ``regex`` -- the same tree that keeps those kinds out of the
    LangGraph engine, and that does not pass the licence policy
    (``DEPENDENCIES.md``, "Known exclusions"). A provider whose client fails
    the gate is not offered until it passes (``docs/specs/agents.md``), so the
    configuration refuses the kind at start-up rather than the engine failing
    at the first turn. Adding it back is this set, a branch in ``chat_model``
    and the dependency -- nothing else.

    It is declared by the port (``robinauts.ports.Agent.kinds``) and answered
    here, so that the composition root asks the engine what it can reach
    instead of importing a second name from this sub-package: deleting the
    adapter must break the line that constructs it and nothing besides
    (``docs/layout.md``, the discard test).
    """

    def __init__(
        self,
        models: ModelsConfig,
        keys: ProviderKeys,
        *,
        model_for: ChatModelFactory = chat_model,
    ) -> None:
        force_tracing_off()
        clear_client_overrides()
        quiet_client_logging()
        self._models = models
        """The models a turn may run on, and the provider each is reached through."""
        self._keys = keys
        """The providers' keys, as start-up read them. It prints nothing."""
        self._model_for = model_for
        self._open = 0

    @property
    def held(self) -> int:
        """How many turns of this engine still hold a stream open.

        Zero once every turn has ended or been closed, which is the promise a
        cancelled run depends on (``robinauts.ports.agents``). It counts the
        engine's own turns; the framework's run and the model's stream live
        inside one and go with it.

        **One engine, one count, however many turns it is running.** A
        deployment shares one ``PydanticAIAgent`` between every conversation,
        so this says "is anything still open", not "is *that* turn still
        open": a caller watching one turn while another is in flight reads the
        other one's stream in this number. Nothing in the platform needs the
        finer answer -- the application releases a turn by closing its own
        stream and never asks -- and the contract suite reads it between turns,
        one at a time, which is when the two questions have the same answer.
        """
        return self._open

    def run_turn(
        self,
        agent: AgentDefinition,
        history: Sequence[Message],
        tools: Sequence[ToolDefinition],
        *,
        model: str,
    ) -> AsyncGenerator[EngineEvent, None]:
        """Answer ``history`` as ``agent`` on ``model``, streaming the events of the turn.

        Not a coroutine and nothing is done here: everything -- building the
        model, building the framework's agent, opening the stream -- happens
        inside the generator, so that a provider that refuses a key is a
        failure of the turn, reported by raising where the caller is iterating,
        and not an exception thrown at whoever asked for the stream.
        """
        return self._turn(agent, history, tuple(tools), model)

    async def _turn(
        self,
        agent: AgentDefinition,
        history: Sequence[Message],
        tools: tuple[ToolDefinition, ...],
        model_id: str,
    ) -> AsyncGenerator[EngineEvent, None]:
        if tools:
            # Binding the definitions to the model is the next step's
            # (``docs/working-notes/mcp-plan.md``, step 4); until then a turn
            # handed tools is refused rather than run without them, which
            # would answer a question the model was meant to use a tool for.
            raise UnsupportedContentError(NO_TOOL_BINDING)
        # The run's model, never the agent's default: the conversation may
        # have been moved to another (``robinauts.ports.agents``).
        model = self._models.model_by_id(model_id)
        provider = self._models.provider_for(model)
        runner = _runner(agent, self._model_for(model, provider, self._keys.key_for(provider.id)))
        settings = _settings(model)
        messages = _messages(history)
        started = False
        streamed: list[str] = []
        self._open += 1
        try:
            async with runner.iter(message_history=messages, model_settings=settings) as run:
                async for node in run:
                    # The user prompt node and everything after the model's
                    # answer are passed over: the first is a question already
                    # in the history, and the rest is where the framework would
                    # retry (see the module docstring).
                    if not FrameworkAgent.is_model_request_node(node):
                        continue
                    async with node.stream(run.ctx) as answering:
                        async for event in answering:
                            text, reasoning = _delta(event)
                            if text or reasoning:
                                if not started:
                                    started = True
                                    yield AnswerStarted()
                                if reasoning:
                                    yield AnswerReasoningDelta(text=reasoning)
                                if text:
                                    streamed.append(text)
                                    yield AnswerTextDelta(text=text)
                    break
            if not started:
                # Nothing was streamed that this version carries -- no text and
                # no thinking. The answer is announced and completed in one
                # breath; an engine is never required to stream
                # (``docs/specs/agents.md``).
                #
                # It is also what an **off-contract** history would reach: one
                # ending in an assistant message leaves the framework with
                # nothing to ask, so no model request node is ever reached and
                # the loop above runs no iterations. The port guarantees a
                # history ending in the user message being answered
                # (``robinauts.ports.agents``), so this is a turn that produced
                # an empty answer rather than one that produced none -- which
                # would be a failed run and is the application's to decide.
                yield AnswerStarted()
            yield AnswerCompleted(parts=_parts(streamed))
        finally:
            # Reached when the turn ends, when it raises, and when the
            # iteration is closed -- which is what a cancellation does. The two
            # context managers above are left on the way out, which is what
            # releases the model's stream and the response under it.
            self._open -= 1


def _runner(agent: AgentDefinition, model: Model) -> FrameworkAgent[None, str]:
    """The framework's agent for one turn: this model, this prompt, no tools.

    **The system prompt is ``instructions``** and not ``system_prompt``: it is
    the agent's, it is taken from the definition as it stands now, and it is
    not one of the messages (``docs/specs/conversations.md``). An empty one is
    left out rather than sent as an empty instruction.

    **No tools and no output type.** This version has none
    (``docs/specs/agents.md``), and the default output of a Pydantic AI agent
    is plain text, which declares no output tool either -- so nothing this
    engine builds gives a model anything to call.

    ``instrument`` is set to ``False`` rather than left alone, which is the
    strongest of the three switches: it beats ``Agent.instrument_all()``, so
    a process where something called ``logfire.instrument_pydantic_ai()``
    still traces nothing of ours (``force_tracing_off``).

    It is given a ``name``, which is also what keeps the framework from
    looking for one in the caller's stack frame at every turn.
    """
    runner: FrameworkAgent[None, str] = FrameworkAgent(
        model=model,
        name=agent.id,
        instructions=agent.system_prompt or None,
    )
    runner.instrument = False
    return runner


def _settings(model: ModelConfig) -> ModelSettings:
    """What the model's configuration says about one request.

    The timeout is per model call and is the configuration's
    (``domain.ModelConfig``); the ceiling is required by Anthropic on every
    request, so the engine sends one whether or not the operator set it
    (``DEFAULT_ANTHROPIC_OUTPUT_TOKENS``). Both travel with the request rather
    than with the client, so that two models of one provider can differ.
    """
    return ModelSettings(
        timeout=model.timeout_seconds,
        max_tokens=model.max_output_tokens or DEFAULT_ANTHROPIC_OUTPUT_TOKENS,
    )


def _messages(history: Sequence[Message]) -> list[ModelMessage]:
    """The history as the framework's messages. The system prompt is not here.

    It is the agent's, it is passed as ``instructions`` (``_runner``), and it
    is deliberately not a message: what goes in ``message_history`` is the
    conversation and nothing else, so the history the model is given is the
    history the platform stored (``docs/specs/conversations.md``).

    **Text only.** Reasoning a previous turn produced is not carried back to
    the model: this version keeps none of it, and what is stored holds none
    either (``docs/working-notes/poc-scope.md``).

    A message with no text at all still becomes a message, empty, because
    dropping it *here* would be this engine deciding what a turn that said
    nothing means. What becomes of it is the **vendor mapping's**, and it is
    the same answer under both engines: Anthropic's API refuses an empty
    content block, so Pydantic AI leaves an empty text part out and then
    leaves out the assistant message it emptied, and langchain-anthropic drops
    an assistant message whose content came out empty. The model is shown the
    same history either way, which is what the swap needs. (Their rules differ
    in one case -- langchain keeps a *trailing* empty assistant message and
    Pydantic AI does not -- and that case cannot arise here: a history ends in
    the user message being answered, ``robinauts.ports.agents``.)

    So the line this adapter draws is that the framework decides, from the
    same input, rather than this adapter deciding for it; if a mapping ever
    changed, it would change under both engines together or be a finding about
    one of them.

    The last of them is the user message being answered, which is what the port
    guarantees -- so the turn is run with no separate prompt and the whole of
    the history in one place.

    **A message of the ``tool`` role is refused.** The role is reserved and not
    carried (``docs/specs/conversations.md``); sending one to a model as though
    the agent had said it would be telling the model something nobody said, and
    quietly. Nothing can put one in a conversation today, which is why this is
    a refusal rather than a translation: the day tool results are stored, this
    is the line that has to learn what they look like.
    """
    messages: list[ModelMessage] = []
    for message in history:
        if message.role not in (Role.USER, Role.ASSISTANT):
            raise UnsupportedContentError(
                f"a message of the {message.role.value} role cannot be sent to a model:"
                f" this version carries no tool results"
            )
        text = "".join(part.text for part in message.parts if isinstance(part, TextPart))
        messages.append(
            ModelRequest(parts=[UserPromptPart(content=text)])
            if message.role is Role.USER
            else ModelResponse(parts=[FrameworkTextPart(content=text)])
        )
    return messages


NO_TOOLS = (
    "the model asked to use a tool, and this version has none: give the agent a"
    " model or a system prompt that does not, or wait for tool usage"
)
"""Why a turn that meets a tool call fails, and what an operator can do."""

NO_TOOL_BINDING = (
    "the Pydantic AI engine does not bind tools to the model yet; a turn with tools is"
    " refused rather than run without them"
)
"""Why a turn handed tool definitions fails, until this engine binds them."""


def _delta(event: ModelResponseStreamEvent) -> tuple[str, str]:
    """The text and the reasoning one stream event carries; ``("", "")`` for none.

    Two of the stream's events say something new: a part **starting**, which
    arrives with the first of its content already in it, and a **delta**, which
    is more of one. Everything else carries nothing -- a part *ending* repeats
    the whole part, and an answer that published it twice would store itself
    twice -- and neither does a part of a kind this version does not carry,
    such as a file or a citation, because an engine that guessed at one would
    be inventing content. An empty piece of either is nothing too: a provider
    that opened a part and put nothing in it has not begun an answer.

    **A tool call is not passed over.** This version has no tools
    (``docs/specs/agents.md``), so a model that asks to use one is asking for
    something that will never happen: every following part would belong to an
    answer that cannot be produced, and passing the call over would finish the
    turn with whatever text happened to come with it -- an answer that looks
    complete and is half of one. An engine reports that by raising, and
    ``UnsupportedContentError`` is what the format already calls content this
    build does not carry. Every spelling of a call is one class
    (``BaseToolCallPart``), a native one and a tool-search one included, so a
    kind the framework adds next is refused rather than passed over.
    """
    if isinstance(event, PartStartEvent):
        part = event.part
        if isinstance(part, BaseToolCallPart):
            raise UnsupportedContentError(NO_TOOLS)
        if isinstance(part, FrameworkTextPart):
            return part.content, ""
        if isinstance(part, ThinkingPart):
            return "", part.content
    elif isinstance(event, PartDeltaEvent):
        delta = event.delta
        if isinstance(delta, ToolCallPartDelta):
            raise UnsupportedContentError(NO_TOOLS)
        if isinstance(delta, TextPartDelta):
            return delta.content_delta, ""
        if isinstance(delta, ThinkingPartDelta):
            return "", delta.content_delta or ""
    return "", ""


def _parts(streamed: list[str]) -> tuple[TextPart, ...]:
    """What the answer completes with: exactly what was streamed.

    **There is no second source, on purpose.** The turn stops at the end of the
    model's first answer, so the framework never assembles a final message this
    could be taken from -- and if it did, taking it would risk storing
    something other than what a person watched arrive, which is the promise
    ``core.check_engine_events`` holds every engine to.

    Made storable on the way (``domain.clean_text``): a provider splits where it
    likes, so a character above the basic plane can arrive in halves, and the
    halves are joined here rather than published as something a store cannot
    hold. An answer with no text at all is one empty part -- which is
    ``text_parts``'s own answer for empty text -- because a message has content
    and "the model said nothing" is worth recording.
    """
    return text_parts(clean_text("".join(streamed)))
