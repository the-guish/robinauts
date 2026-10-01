# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""One turn on Pydantic AI: the framework's agent runs the loop, the adapter streams it.

The second implementation of the ``Agent`` port (``robinauts.legacy.ports.agents``),
and the only module in the platform that may name Pydantic AI
(``docs/layout.md``, enforced by the import contracts in
``backend/pyproject.toml``). Everything about the framework stops here: what
crosses the port is a question and the conversation's memory in, and the
adapter's events out, and the **discard test** is that three places name this
sub-package and deleting it and its dependencies breaks those and nothing
else: its import in ``robinauts.legacy.app`` and its one entry in ``ENGINES``; the
contract exceptions in ``backend/pyproject.toml`` that name the sub-package;
and this sub-package's own tests. The composition tests
(``tests/unit/test_app_composition.py``) fail too and name no adapter: they
say that *both* engines are wired, which is a claim about the table and not
about either sub-package.

**The framework owns the loop, the context and the memory** (ADR 0005). A
Pydantic AI ``Agent`` is built for the turn -- the model, the toolsets of the
agent's MCP servers through the framework's own MCP client, the instructions,
and the history processor that keeps the context within the window -- and
``run_stream_events`` runs the whole turn: the model asks for a tool, the
framework calls it, the result goes back, the model answers, as many times as
the turn needs. What this adapter does is translate what it emits, exactly as
the Pydantic AI backend of `agent-framework-examples
<https://github.com/the-guish/agent-framework-examples>`_ does:

- a ``PartStartEvent`` carries the first of a part and a ``PartDeltaEvent``
  more of it: text becomes ``TextDelta`` and thinking ``ReasoningDelta``;
- a ``FunctionToolCallEvent`` is a call the framework is about to make, and
  becomes ``ToolCall`` with the vendor's id, the tool's name and its
  arguments whole -- there is no reason to stream the arguments of a call the
  framework runs itself;
- a ``FunctionToolResultEvent`` is what the tool answered, and becomes
  ``ToolResult``: a return the tool marked as failed, and a retry prompt the
  framework wrote for a call it could not validate, are results with
  ``is_error`` set, since the model reads both;
- the ``AgentRunResultEvent`` ends the turn: its output is what the turn is
  ``Done`` with, and its messages are the **memory**, serialised by the
  framework's own type adapter and handed back as the state the platform
  stores against the run and hands to the next turn
  (``docs/specs/conversations.md``).

**The system prompt is ``instructions=``, not ``system_prompt=``.** The two
differ in exactly the way that matters here: a ``system_prompt`` becomes a
part of the message history, carried along with it and replayed, while
``instructions`` are taken from the agent's definition at every run. Editing
an agent must take effect at the next turn of its existing conversations
(``docs/specs/agents.md``), which is what ``instructions`` means.

**The context is kept within the window** by a history processor
(``within``): before every model call, and again on the memory handed back,
the oldest exchanges are dropped until what is left fits the share of the
window the turn may spend (``TRIM_AT``). An exchange is a question and
everything up to the next one, so a tool call is never parted from its
result. The window is the model's configured ``context_window``, then what
the framework's profile says of the model, then ``DEFAULT_CONTEXT_WINDOW``.

**The vendor's cache is asked for** on every request (``settings``): the
instructions, the tool definitions and the conversation so far are marked
cacheable, which is how a long conversation stops paying for its whole
history at every turn.

**Two protocols, four kinds** (``PydanticAIAgent.kinds``). ``AnthropicModel``
speaks Anthropic's Messages API, to Anthropic or to an ``anthropic-compatible``
endpoint; ``OpenAIChatModel`` speaks OpenAI's **Chat Completions** -- never the
Responses API, which is ``OpenAIResponsesModel`` and is not built here -- to
OpenAI or to an ``openai-compatible`` endpoint (``chat_model``). Everything
past the model -- the agent, the loop, the stream, the mapping, the memory --
is one path for both.

**Nothing phones home** (``docs/specs/core.md``). Pydantic AI's
instrumentation is turned off explicitly on every agent it builds
(``force_tracing_off``, ``instrument = False``), so no tracer, no exporter and
no Logfire client is ever made whatever the environment says; and a vendor's
client is built from the configuration rather than from the environment --
the endpoint, the key and the key's header (``ANTHROPIC_ENDPOINT``,
``OPENAI_ENDPOINT``, ``clear_client_overrides``).

**And nothing is written down either.** The platform's logs never carry
conversation content: the vendor SDKs' loggers that write request bodies --
which on ``ANTHROPIC_LOG`` or ``OPENAI_LOG``, and on any root logger turned up
afterwards, put every request's messages and system prompt on standard error
-- are pinned at ``WARNING`` when the engine is built (``quiet_client_logging``).

**Failure and cancellation** are the port's. Whatever the provider, a tool
server or the framework raises travels out of the generator as it is;
``CancelledError`` is never swallowed; and the ``finally`` leaves the
framework's context manager, which is what lets go of the model's stream, the
HTTP response underneath it and the tool sessions. A turn that raised hands
back no memory: the conversation resumes from the one it had.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncGenerator, Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any, cast

import pydantic_ai
from anthropic import AsyncAnthropic
from openai import AsyncOpenAI
from openai.types.chat import chat_completion_chunk
from pydantic import ValidationError
from pydantic_ai import Agent as FrameworkAgent
from pydantic_ai import ModelSettings, UsageLimits
from pydantic_ai.capabilities import ProcessHistory
from pydantic_ai.mcp import MCPToolset
from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponseStreamEvent,
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
    ThinkingPart,
    ThinkingPartDelta,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models import Model
from pydantic_ai.models.anthropic import AnthropicModel, AnthropicModelSettings
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIStreamedResponse
from pydantic_ai.profiles import ModelProfile
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.run import AgentRunResultEvent
from pydantic_ai.toolsets import AbstractToolset

from robinauts.legacy.adapters.config_file import ProviderKeys, ToolServerSecrets, credential_header
from robinauts.legacy.domain import (
    MAX_PART_CHARS,
    AgentDefinition,
    ConfigError,
    Done,
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
    ToolServerConfig,
)
from robinauts.legacy.ports import Agent

_log = logging.getLogger(__name__)

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

OPENAI_ENDPOINT = "https://api.openai.com/v1"
"""Where OpenAI is, said here rather than left to the client to decide.

The same rule as ``ANTHROPIC_ENDPOINT``, for the other vendor: the OpenAI SDK
falls back to ``OPENAI_BASE_URL`` when no base URL is passed, and a variable
inherited from a shell or an image would send every turn and the operator's
key to the host it named. So the ``openai`` kind is reached here and nowhere
else, and it has no ``base_url`` to offer (``domain.KINDS_WITH_BASE_URL``).

Spelt, unlike Anthropic's, **with** the ``/v1``: what OpenAI's client appends
to a base URL is ``/chat/completions`` alone, so the version is part of the
prefix -- which is also what an ``openai-compatible`` provider's ``base_url``
is (``endpoint_of``).
"""

ANTHROPIC_KINDS: frozenset[ProviderKind] = frozenset(
    {ProviderKind.ANTHROPIC, ProviderKind.ANTHROPIC_COMPATIBLE}
)
"""The kinds that speak Anthropic's Messages API, reached through ``AnthropicModel``."""

OPENAI_KINDS: frozenset[ProviderKind] = frozenset(
    {ProviderKind.OPENAI, ProviderKind.OPENAI_COMPATIBLE}
)
"""The kinds that speak OpenAI's Chat Completions, reached through ``OpenAIChatModel``.

Which model a turn is given is decided by these two sets and nothing else
(``chat_model``), and a kind in neither is refused rather than handed to
whichever client is nearer.
"""

COMPATIBLE_KINDS: frozenset[ProviderKind] = frozenset(
    {ProviderKind.ANTHROPIC_COMPATIBLE, ProviderKind.OPENAI_COMPATIBLE}
)
"""The two kinds whose endpoint is the operator's ``base_url`` (``endpoint_of``).

Written out here rather than read off ``domain.KINDS_WITH_BASE_URL``, which is
the same two today: that set says which kinds the configuration gives an
address, and this one which addresses this engine has a client to send to.
"""

TRACING_VARIABLES_REMOVED: tuple[str, ...] = ()
"""What ``force_tracing_off`` takes out of the environment: nothing.

Deliberately empty, and named all the same so that the claim is written down
rather than merely true today. Pydantic AI has **no environment switch for
instrumentation**: it traces when, and only when, an agent's ``instrument``
says so -- set by ``logfire.instrument_pydantic_ai()``, by
``Agent.instrument_all()`` or per agent -- and the engine sets it to ``False``
on every agent it builds, which beats both of the others. Nothing is read
from ``LOGFIRE_TOKEN``, ``LOGFIRE_SEND_TO_LOGFIRE``,
``OTEL_EXPORTER_OTLP_ENDPOINT``, ``OTEL_TRACES_EXPORTER`` or any
``PYDANTIC_AI_*`` variable on the path a turn takes: an ``OTel`` tracer
provider is asked for only while *building* instrumentation settings, which
never happens. So there is nothing here that an argument cannot say, and
editing a process's environment for the sake of saying it again would be an
adapter reaching further than it needs to (compare the LangGraph adapter,
where a variable really is load-bearing).
"""

CLIENT_VARIABLES_REMOVED = (
    "ANTHROPIC_CUSTOM_HEADERS",
    "ANTHROPIC_LOG",
    "OPENAI_CUSTOM_HEADERS",
    "OPENAI_LOG",
    "OPENAI_ORG_ID",
    "OPENAI_PROJECT_ID",
    "OPENAI_ADMIN_KEY",
)
"""The variables ``clear_client_overrides`` takes out of the environment.

The ones the two clients read that **no argument can override**; the endpoint
and the key are arguments, and an argument wins.

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

The OpenAI SDK, which this engine builds its OpenAI client from, has the same
two and three more. ``OPENAI_CUSTOM_HEADERS`` is merged exactly as Anthropic's
is -- the key travels in ``Authorization`` there, and an ``Authorization`` line
in the variable is dropped only when the caller passed one, which
``chat_model`` always does (``OPENAI_KEY_HEADER``); anything else in it would
be sent on every turn. ``OPENAI_LOG`` is read at import, as ``ANTHROPIC_LOG``
is, and on ``debug`` puts the ``openai`` logger at ``DEBUG`` and calls
``logging.basicConfig()``; this version of the SDK writes no request body
there, but the promise is not left to rest on what one version happens to log.
``OPENAI_ORG_ID`` and ``OPENAI_PROJECT_ID`` are read when the client is built
and not given, and ``None`` -- the only way to say "none" -- *is* not given,
so no argument can keep an ``OpenAI-Organization`` or ``OpenAI-Project``
header nobody configured off a turn. And ``OPENAI_ADMIN_KEY`` is a second
credential the client picks up whenever it is not passed one, and holds for
the life of the client: not what a chat request is signed with, and still a
key the operator did not configure for this provider.

Removed once, at construction, and never per turn: a process-wide edit made
while turns are running would be one turn changing another's environment.

What is **not** here, because an argument does say it or because nothing on a
turn's path reads it: ``OPENAI_API_KEY`` and ``OPENAI_BASE_URL`` (the key and
the endpoint are passed; ``OpenAIProvider`` looks at both only when it builds a
client, and it is handed one), ``OPENAI_WEBHOOK_SECRET`` (read into the client
and used only to verify a webhook, which the platform never receives), and the
Azure and Bedrock variables (``AZURE_OPENAI_*``, ``OPENAI_API_TYPE``,
``OPENAI_API_VERSION``, ``AWS_*``), which only the SDK's module-level client
and its Azure and Bedrock clients read, none of them ever built.
"""

QUIET_CLIENT_LOGGERS = (
    "anthropic",
    "anthropic._base_client",
    "openai",
    "openai._base_client",
    "httpx2",
    "httpcore2",
)
"""The loggers ``quiet_client_logging`` pins, and why these six.

``anthropic`` is the SDK's own, and ``anthropic._base_client`` is the module
that **emits** the record carrying a request's ``json_data`` -- the system
prompt and every message. The child is named as well as the parent because a
level set on a child is what a logger decides by: an ``anthropic._base_client``
entry in somebody's ``dictConfig`` would walk straight past a pin on
``anthropic``. ``httpx2`` is the HTTP client the SDK is built on -- a fork of
its own, so this is **not** the ``httpx`` the sign-in adapter uses and pinning
it silences nothing of ours -- and ``httpcore2`` is the connection layer under
that, which logs request lines and headers.

``openai`` and ``openai._base_client`` are the same pair in the other vendor's
SDK, which is built on the same ``httpx2``. This version of it writes no request
body at ``DEBUG``, but ``OPENAI_LOG`` still turns the pair on for the whole
process, and a pin is cheaper than a promise that holds until an upgrade.

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

OPENAI_KEY_HEADER = "Authorization"
"""The header OpenAI's key travels in, as ``Bearer <key>``, pinned by ``chat_model``.

The same belt as ``ANTHROPIC_KEY_HEADER``, and a little more than a belt: the
OpenAI SDK drops an ``Authorization`` line of ``OPENAI_CUSTOM_HEADERS`` only
when the caller passed an ``Authorization`` header of its own, so passing one is
what makes the configured key the only one that can be sent.
"""

MAX_RETRIES = 0
"""How many times a failed model call is retried by the client: not at all.

The client's own retries are invisible to everything above -- they would
lengthen a turn silently and could send the same prompt twice after a timeout
the provider had already accepted. A turn is bounded by the application
(``robinauts.legacy.application.Turns``), a failure is reported by raising, and
retrying is sending the message again (``docs/specs/runs.md``).

**What this does not switch off is the framework's own retrying**, which is
the loop's and is now wanted: a tool call the framework could not validate
is sent back to the model as a retry prompt, once, before the turn fails; and
for the models whose thinking blocks Anthropic binds to a conversation, a
replayed block the vendor refuses is sent once more marked to be dropped.
"""

DEFAULT_ANTHROPIC_OUTPUT_TOKENS = 8192
"""What Anthropic is asked for when the model's configuration says nothing.

Its API requires a ceiling on every request, so an engine that sent none would
not work at all; the number is the client's business rather than the
platform's, which is why ``ModelConfig.max_output_tokens`` defaults to "leave
it to the engine" and this is where "the engine" answers.
"""

DEFAULT_OPENAI_OUTPUT_TOKENS: int | None = None
"""What an OpenAI-protocol provider is asked for when the configuration says nothing: no ceiling.

Chat Completions requires none, and the model's own limit is then the
ceiling. That is the engine's answer rather than a number of its own because
on OpenAI's reasoning models the ceiling covers the reasoning tokens as well
as the answer, so a ceiling picked to bound an answer's length can be spent
entirely on thinking and leave an empty answer. An operator who wants a bound
writes ``max_output_tokens``, and it is sent in the field the kind takes
(``_as_chat_completions``). The other engine sends the same
(``docs/specs/agents.md``).
"""

MAX_TOOL_ROUNDS = 25
"""How many times one turn may go back to the model with tool results.

A bound on the loop, so that a model that keeps calling tools cannot run a
turn for ever on the operator's account; the framework's request limit is set
from it (``REQUEST_LIMIT``), and a turn that reaches it fails with what the
framework says. Twenty-five rounds is far past what a turn that is getting
somewhere needs and far short of what a turn that is not would spend.
"""

REQUEST_LIMIT = MAX_TOOL_ROUNDS + 1
"""The framework's bound on a turn: one model request per round, and the first."""

DEFAULT_CONTEXT_WINDOW = 200_000
"""The context window assumed for a model nobody said the window of.

For a model the framework has no profile for -- a gateway's model id -- and
no ``context_window`` in the configuration. Haiku 4.5's, and conservative
for a bigger model: trimming too early loses history, trimming too late costs
a refused request.
"""

TRIM_AT = 0.8
"""The share of the window the history may fill before the oldest exchanges go."""

CHARS_PER_TOKEN = 4
"""How the history is measured without a tokeniser: a token is about four characters.

Rough, and on the side of trimming early: what it measures is the message
serialised whole, which is longer than what the vendor tokenises.
"""


ChatModelFactory = Callable[[ModelConfig, ModelProviderConfig, str], Model]
"""How a model is built: the model, its provider, and the provider's key.

Injectable so that the tests run the engine over one of Pydantic AI's own test
models -- the agent, its loop, its streaming and its releasing are then
exercised for real with no network and no key (``docs/layout.md``, "Testing
strategy").
"""

ToolsetsFactory = Callable[
    [Sequence[ToolServerConfig], ToolServerSecrets], Sequence[AbstractToolset[Any]]
]
"""How the toolsets of the agent's servers are built for a turn.

Injectable for the same reason: the tests hand the engine plain functions as
a toolset, as the examples do, and the framework runs them exactly as it runs
a server's.
"""

HistoryProcessor = Callable[[list[ModelMessage]], Awaitable[list[ModelMessage]]]
"""What ``within`` hands the framework: the messages, kept to the window."""


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

    Two of those three are answered where an agent is built: the engine sets
    ``instrument = False`` on every agent it makes, which **beats** the
    process-wide default, and the model comes from this adapter's own factory
    and is never an instrumented one. There is deliberately no environment
    variable to unset, because there is none to read
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
    not from the environment" (``ANTHROPIC_ENDPOINT``, ``OPENAI_ENDPOINT``). An
    endpoint and a key are arguments, and an argument wins; **headers are
    merged**, and an organisation, a project and an admin key are read
    whenever the argument is ``None`` -- which is the only way to pass
    "none" -- so the only way to say "and nothing else" is for the variable not
    to be there (``CLIENT_VARIABLES_REMOVED``).

    An adapter may touch the environment -- it is the layer that may -- and
    this does it once, when the engine is built at start-up, so that no turn
    ever edits the environment another turn is reading. ``ANTHROPIC_LOG`` and
    ``OPENAI_LOG`` go with them so that a subprocess this deployment starts
    does not import an SDK and turn its own logging on again; what protects
    **this** process, where the imports have already happened, is
    ``quiet_client_logging``.
    """
    for name in CLIENT_VARIABLES_REMOVED:
        os.environ.pop(name, None)


def quiet_client_logging() -> None:
    """Hold the vendor SDKs' loggers at ``WARNING``: no request is ever a record.

    **The platform's logs never carry conversation content, and never a key.**
    The Anthropic SDK reads ``ANTHROPIC_LOG`` at *import* -- before anything
    here exists -- and on ``debug`` puts its own logger and its HTTP client's
    at ``DEBUG`` and calls ``logging.basicConfig()``, which attaches a handler
    to the root logger if nothing else has. What the SDK's logger then writes
    for every call is "Request options: ..." with the request's ``json_data``
    in it: the agent's system prompt and every message of the conversation, on
    standard error. The OpenAI SDK reads ``OPENAI_LOG`` the same way, and is
    pinned the same way.

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
    """Where that provider is: the address the operator gave, or the vendor's own.

    The four kinds this engine reaches are two **vendors** and two
    **protocols** (``PydanticAIAgent.kinds``). ``anthropic`` is Anthropic, at
    ``ANTHROPIC_ENDPOINT`` and nowhere else, and ``openai`` is OpenAI, at
    ``OPENAI_ENDPOINT``; neither has a ``base_url`` to offer.
    ``anthropic-compatible`` and ``openai-compatible`` are endpoints the
    operator names that speak the same protocol as the vendor -- OpenRouter
    serves both, a vLLM server or a gateway the second -- so the address is
    theirs, checked where every configured endpoint is
    (``domain.is_endpoint_url``: https, or http on the loopback interface, no
    query, no fragment and no credential written into it).

    **A base URL is a prefix the client appends the protocol's own path to**,
    and the two protocols' paths differ. Anthropic's client appends
    ``/v1/messages``, so an operator reaching OpenRouter that way writes
    ``https://openrouter.ai/api`` and the request goes to
    ``https://openrouter.ai/api/v1/messages``; OpenAI's appends
    ``/chat/completions``, so the version is the operator's to write, and the
    same OpenRouter over OpenAI's protocol is ``https://openrouter.ai/api/v1``,
    reached at ``https://openrouter.ai/api/v1/chat/completions``
    (``docs/specs/agents.md``).

    A provider of any other kind should not arrive here: the configuration was
    held to ``PydanticAIAgent.kinds`` at start-up. One that does -- a kind added
    to the platform's vocabulary without a branch here, a caller that built a
    definition by hand -- gets a refusal naming it, and never a turn sent to
    whichever endpoint happened to be nearest. Every kind there is today has a
    branch, so the tests reach this one with a kind of their own, which is what
    keeps it a branch that is exercised rather than merely written.

    Spelt out again here rather than imported from the LangGraph adapter: the
    two adapters do not import each other (``docs/layout.md``), and deleting
    either must leave the other whole.
    """
    if provider.kind is ProviderKind.ANTHROPIC:
        return ANTHROPIC_ENDPOINT
    if provider.kind is ProviderKind.OPENAI:
        return OPENAI_ENDPOINT
    if provider.kind in COMPATIBLE_KINDS and provider.base_url:
        return provider.base_url
    raise _unreachable(provider)


def _unreachable(provider: ModelProviderConfig) -> ConfigError:
    """The refusal for a provider this engine has no client for, naming it."""
    return ConfigError(
        [
            f"model_providers.{provider.id}: this build of the Pydantic AI engine cannot"
            f" reach a {provider.kind.value} provider"
        ]
    )


def chat_model(model: ModelConfig, provider: ModelProviderConfig, key: str) -> Model:
    """The model that model's configuration describes.

    The key is passed in and not read here: what may touch the environment is
    ``robinauts.legacy.adapters.config_file``, and what reaches this is one key for
    one call (``docs/specs/agents.md``).

    **The provider's kind chooses the model, and nothing else does**: the two
    Anthropic kinds get ``AnthropicModel``, described below, and the two
    OpenAI kinds ``OpenAIChatModel`` (``_openai_model``). A kind in neither set
    is refused, naming the provider.

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

    The timeout, the output ceiling and the cache are **not** here: they
    travel with the request, as the model settings (``settings``), which is
    where the model's configuration reaches a turn.
    """
    if provider.kind in OPENAI_KINDS:
        return _openai_model(model, provider, key)
    if provider.kind not in ANTHROPIC_KINDS:
        raise _unreachable(provider)
    client = AsyncAnthropic(
        api_key=key,
        base_url=endpoint_of(provider),
        max_retries=MAX_RETRIES,
        default_headers={ANTHROPIC_KEY_HEADER: key},
    )
    return AnthropicModel(model.name, provider=AnthropicProvider(anthropic_client=client))


def _openai_model(model: ModelConfig, provider: ModelProviderConfig, key: str) -> Model:
    """``OpenAIChatModel`` over a client this adapter built itself.

    **The protocol is Chat Completions, for both kinds**: the framework's
    ``OpenAIChatModel``, and never its ``OpenAIResponsesModel``. An
    ``openai-compatible`` endpoint -- a gateway, vLLM, OpenRouter -- speaks
    Chat Completions, one protocol for both kinds keeps the two kinds and the
    two engines symmetric, and the LangGraph engine reaches the same
    endpoints through ``ChatOpenAI`` (``docs/specs/agents.md``). The
    Responses API is a decision of its own.

    **The client is built here and handed to the provider**, for the reason
    Anthropic's is: what must be pinned is the client's. The key, so that
    ``OPENAI_API_KEY`` is never consulted -- ``OpenAIProvider`` looks at it,
    and at ``OPENAI_BASE_URL``, only when it builds a client of its own, which
    it is not asked to; the endpoint (``endpoint_of``); no retries
    (``MAX_RETRIES``); and the key pinned as its header
    (``OPENAI_KEY_HEADER``). What the SDK reads when an argument is ``None`` --
    the organisation, the project, the admin key, the headers -- is out of the
    environment by the time a turn runs (``clear_client_overrides``). There is
    no vendor-specific proxy variable in this SDK either.

    The timeout and the ceiling travel with the request (``settings``), the
    ceiling in the field the kind takes (``_as_chat_completions``), and the
    stream is read through ``_ChatCompletionsStream``.
    """
    client = AsyncOpenAI(
        api_key=key,
        base_url=endpoint_of(provider),
        max_retries=MAX_RETRIES,
        default_headers={OPENAI_KEY_HEADER: f"Bearer {key}"},
    )
    compatible = provider.kind is ProviderKind.OPENAI_COMPATIBLE

    def profile(given: ModelProfile) -> ModelProfile:
        return _as_chat_completions(given, compatible=compatible)

    return _ChatCompletionsModel(
        model.name, provider=OpenAIProvider(openai_client=client), profile=profile
    )


@dataclass
class _ChatCompletionsStream(OpenAIStreamedResponse):
    """The framework's Chat Completions stream, taking a repeated tool name as one.

    Through the hook the framework documents for it (``_map_tool_call_delta``)
    and its public lookup of a part by the vendor's index: **a name sent again
    is not more of the name.** The framework appends every ``name`` a call's
    deltas carry, so a server that repeats the id and the name on every delta
    -- some compatible ones do -- would have the call stored as
    ``github_searchgithub_search``, and the framework would then tell the
    model there is no such tool. A name equal to the one the call already has
    is dropped before the framework sees it; a name that differs is left to
    arrive, and is the framework's to make what it will of.

    A delta may carry no ``index``: OpenAI's always do, and a compatible
    server's need not, in which case the framework keeps the call as "the
    latest call", and that is the one whose name is compared.
    """

    def _map_tool_call_delta(
        self, choice: chat_completion_chunk.Choice
    ) -> Iterable[ModelResponseStreamEvent]:
        for delta in choice.delta.tool_calls or []:
            if delta.function is None or not delta.function.name:
                continue
            if delta.index is not None:
                known: object = self._parts_manager.get_part_by_vendor_id(delta.index)
            else:
                whole = self._parts_manager.get_parts()
                known = whole[-1] if whole else None
            if isinstance(known, ToolCallPart) and known.tool_name == delta.function.name:
                delta.function.name = None
        yield from super()._map_tool_call_delta(choice)


class _ChatCompletionsModel(OpenAIChatModel):
    """The framework's Chat Completions model, streaming through ``_ChatCompletionsStream``.

    ``_streamed_response_cls`` is the framework's documented seam for a
    stream of one's own; nothing else about the model is changed.
    """

    @property
    def _streamed_response_cls(self) -> type[OpenAIStreamedResponse]:
        return _ChatCompletionsStream


def _as_chat_completions(profile: ModelProfile, *, compatible: bool) -> ModelProfile:
    """The framework's OpenAI profile, with the ceiling in the field the kind takes.

    ``max_completion_tokens`` for OpenAI itself, ``max_tokens`` for an
    ``openai-compatible`` endpoint, which is the field OpenRouter and older
    servers know -- the framework's own OpenRouter profile makes the same
    choice. Everything else the profile says about the model is kept: the
    framework's schema transformer, its thinking tags, its tool support.
    """
    if not compatible:
        return profile
    return cast(ModelProfile, {**profile, "openai_chat_supports_max_completion_tokens": False})


def settings(model: ModelConfig, provider: ModelProviderConfig) -> ModelSettings:
    """What the model's configuration says about one request, and the cache it asks for.

    The timeout is per model call and is the configuration's
    (``domain.ModelConfig``). The ceiling is required by Anthropic on every
    request, so over Anthropic's protocol the engine sends one whether or not
    the operator set it (``DEFAULT_ANTHROPIC_OUTPUT_TOKENS``); over OpenAI's it
    is sent only when the operator set one (``DEFAULT_OPENAI_OUTPUT_TOKENS``),
    in the field the kind takes (``_as_chat_completions``). Both travel with
    the request rather than with the client, so that two models of one
    provider can differ.

    The cache, over Anthropic's protocol: the instructions and the tool
    definitions are marked, and so is the conversation -- with the vendor's
    own automatic breakpoint, which it moves forward as the conversation
    grows, or, at a gateway that speaks the Messages API without that
    top-level parameter, with a breakpoint on the last message. OpenAI caches
    a prompt's prefix by itself and takes no marking. The framework ignores
    every vendor's key for a model that is not that vendor's, which is what
    lets a scripted model take the same settings in the tests.
    """
    if provider.kind in OPENAI_KINDS:
        ceiling = model.max_output_tokens or DEFAULT_OPENAI_OUTPUT_TOKENS
        if ceiling is None:
            return ModelSettings(timeout=model.timeout_seconds)
        return ModelSettings(timeout=model.timeout_seconds, max_tokens=ceiling)
    at_the_vendor = provider.kind is ProviderKind.ANTHROPIC
    return AnthropicModelSettings(
        timeout=model.timeout_seconds,
        max_tokens=model.max_output_tokens or DEFAULT_ANTHROPIC_OUTPUT_TOKENS,
        anthropic_cache_instructions=True,
        anthropic_cache_tool_definitions=True,
        anthropic_cache=at_the_vendor,
        anthropic_cache_messages=not at_the_vendor,
    )


def mcp_toolset(server: ToolServerConfig, secrets: ToolServerSecrets) -> AbstractToolset[Any]:
    """That server as the framework's client connects to it, its tools under its id.

    The endpoint is the configured one and nothing else, the credential is the
    one start-up read for the server (``credential_header``: ``Bearer``,
    ``Basic``, or no header at all under ``auth = "none"``), and the server's
    ``timeout_seconds`` bounds the connection and every read from it.
    ``HTTPS_PROXY`` is obeyed by the client's ``httpx`` as by every other
    outbound call of the process (``docs/specs/agents.md``, "Tools").

    The tools are named ``<server id>_<tool>`` (``prefixed``) so that two
    servers offering ``search`` never collide, and a tool the server says
    failed is a **failed result the model reads** rather than a failure of
    the turn or a retry charged against the tool (``tool_error_behavior``);
    a server that cannot be reached raises, which fails the turn. Nothing
    connects here: the framework opens the session at the first turn that
    lists or calls a tool, and closes it with the run.
    """
    return MCPToolset(
        server.url,
        id=server.id,
        headers=credential_header(server, secrets) or None,
        init_timeout=server.timeout_seconds,
        read_timeout=server.timeout_seconds,
        tool_error_behavior="failed",
    ).prefixed(server.id)


def mcp_toolsets(
    servers: Sequence[ToolServerConfig], secrets: ToolServerSecrets
) -> Sequence[AbstractToolset[Any]]:
    """The toolsets of those servers, one each, in the agent's order."""
    return [mcp_toolset(server, secrets) for server in servers]


class PydanticAIAgent(Agent):
    """The Pydantic AI engine: one turn, one agent, built and thrown away."""

    kinds: frozenset[ProviderKind] = ANTHROPIC_KINDS | OPENAI_KINDS
    """The provider kinds this build of the engine has a client for: all four.

    Two clients, two kinds each. ``AsyncAnthropic`` reaches Anthropic itself
    and any endpoint that speaks Anthropic's Messages API at an address the
    operator gives; ``AsyncOpenAI`` reaches OpenAI itself and any endpoint that
    speaks OpenAI's Chat Completions (``endpoint_of``, ``chat_model``).
    OpenRouter speaks both, so it may be configured as either kind -- the
    Messages API with ``https://openrouter.ai/api``, or Chat Completions with
    ``https://openrouter.ai/api/v1`` -- and they are two providers, not two
    spellings of one.

    A kind this engine does not build is not offered (``docs/specs/agents.md``),
    so the configuration would refuse it at start-up rather than the engine
    failing at the first turn; today there is no such kind. A new one is this
    set, a branch in ``endpoint_of`` and ``chat_model``, and its client's
    dependency -- nothing else.

    It is declared by the port (``robinauts.legacy.ports.Agent.kinds``) and answered
    here, so that the composition root asks the engine what it can reach
    instead of importing a second name from this sub-package: deleting the
    adapter must break the line that constructs it and nothing besides
    (``docs/layout.md``, the discard test).
    """

    def __init__(
        self,
        models: ModelsConfig,
        keys: ProviderKeys,
        tool_secrets: ToolServerSecrets | None = None,
        *,
        model_for: ChatModelFactory = chat_model,
        toolsets_for: ToolsetsFactory = mcp_toolsets,
    ) -> None:
        force_tracing_off()
        clear_client_overrides()
        quiet_client_logging()
        self._models = models
        """The models a turn may run on, the provider each is reached through,
        and the tool servers an agent may name."""
        self._keys = keys
        """The providers' keys, as start-up read them. It prints nothing."""
        self._tool_secrets = tool_secrets if tool_secrets is not None else ToolServerSecrets({})
        """The tool servers' secrets, likewise."""
        self._model_for = model_for
        self._toolsets_for = toolsets_for
        self._open = 0

    @property
    def held(self) -> int:
        """How many turns of this engine still hold a stream open.

        Zero once every turn has ended or been closed, which is the promise a
        cancelled run depends on (``robinauts.legacy.ports.agents``). It counts the
        engine's own turns; the framework's run, the model's stream and the
        tool sessions live inside one and go with it.

        **One engine, one count, however many turns it is running.** A
        deployment shares one ``PydanticAIAgent`` between every conversation,
        so this says "is anything still open", not "is *that* turn still
        open"; the contract suite reads it between turns, one at a time, which
        is when the two questions have the same answer.
        """
        return self._open

    def stream(
        self,
        agent: AgentDefinition,
        prompt: str,
        *,
        model: str,
        state: bytes | None,
    ) -> AsyncGenerator[Event, None]:
        """Send ``prompt`` as the next user turn and stream the turn's events.

        Not a coroutine and nothing is done here: everything -- building the
        model, building the framework's agent, opening the stream -- happens
        inside the generator, so that a provider that refuses a key is a
        failure of the turn, reported by raising where the caller is iterating,
        and not an exception thrown at whoever asked for the stream.
        """
        return self._turn(agent, prompt, model, state)

    async def _turn(
        self, agent: AgentDefinition, prompt: str, model_id: str, state: bytes | None
    ) -> AsyncGenerator[Event, None]:
        # The run's model, never the agent's default: the conversation may
        # have been moved to another (``robinauts.legacy.ports.agents``).
        model = self._models.model_by_id(model_id)
        provider = self._models.provider_for(model)
        client = self._model_for(model, provider, self._keys.key_for(provider.id))
        servers = [self._models.tool_servers[server_id] for server_id in agent.tools]
        history = read_state(state)
        kept_within = within(context_window(client, model))
        text, remembered = "", history
        self._open += 1
        try:
            runner = runner_for(
                agent, client, list(self._toolsets_for(servers, self._tool_secrets)), kept_within
            )
            async with runner.run_stream_events(
                prompt,
                message_history=history,
                model_settings=settings(model, provider),
                usage_limits=UsageLimits(request_limit=REQUEST_LIMIT),
            ) as events:
                async for event in events:
                    if isinstance(event, AgentRunResultEvent):
                        text, remembered = event.result.output, event.result.all_messages()
                    else:
                        for made in _events_of(event):
                            yield made
            yield Done(text=text, state=write_state(await kept_within(list(remembered))))
        finally:
            # Reached when the turn ends, when it raises, and when the
            # iteration is closed -- which is what a cancellation does. The
            # context manager above is left on the way out, which is what
            # releases the model's stream and the response under it.
            self._open -= 1


def runner_for(
    agent: AgentDefinition,
    client: Model,
    toolsets: Sequence[AbstractToolset[Any]],
    kept_within: HistoryProcessor,
) -> FrameworkAgent[None, str]:
    """The framework's agent for one turn: this model, these instructions, these tools.

    **The system prompt is ``instructions``** and not ``system_prompt``: it is
    the agent's, it is taken from the definition as it stands now, and it is
    not one of the messages (``docs/specs/conversations.md``). An empty one is
    left out rather than sent as an empty instruction. **No output type**: the
    default output of a Pydantic AI agent is plain text, which declares no
    output tool.

    ``instrument`` is set to ``False`` rather than left alone, which is the
    strongest of the three switches: it beats ``Agent.instrument_all()``, so
    a process where something called ``logfire.instrument_pydantic_ai()``
    still traces nothing of ours (``force_tracing_off``).

    It is given a ``name``, which is also what keeps the framework from
    looking for one in the caller's stack frame at every turn.
    """
    runner: FrameworkAgent[None, str] = FrameworkAgent(
        model=client,
        name=agent.id,
        instructions=agent.system_prompt or None,
        toolsets=list(toolsets) or None,
        capabilities=[ProcessHistory(kept_within)],
    )
    runner.instrument = False
    return runner


def context_window(client: Model, model: ModelConfig) -> int:
    """The window the context is kept within, in tokens."""
    if model.context_window is not None:
        return model.context_window
    known = client.profile.get("context_window")
    if isinstance(known, int) and known > 0:
        return known
    return DEFAULT_CONTEXT_WINDOW


def within(window: int) -> HistoryProcessor:
    """A history processor that keeps the messages to that window.

    The oldest **exchanges** go first -- a question and everything up to the
    next one, so that a tool call is never parted from its result -- until
    what is left measures under ``TRIM_AT`` of the window; the latest
    exchange stays whatever it measures, since a turn with nothing in front
    of the model is no turn. What the framework is handed before each model
    call is what the memory is trimmed to on the way out, so the platform
    stores what the model saw and no more.
    """
    budget = window * TRIM_AT

    async def kept(messages: list[ModelMessage]) -> list[ModelMessage]:
        starts = [at for at, message in enumerate(messages) if _begins_an_exchange(message)]
        if len(starts) < 2:
            return messages
        # Whatever precedes the first question goes with it.
        ends = [*starts[1:], len(messages)]
        measured = 0.0
        # From the newest exchange back: the first that does not fit, and
        # everything before it, is what goes.
        for start, end in reversed(list(zip([0, *starts[1:]], ends, strict=True))):
            measured += sum(_measured(message) for message in messages[start:end])
            if measured > budget and end != len(messages):
                _log.info("the history was trimmed to the window: %d messages dropped", end)
                return messages[end:]
        return messages

    return kept


def _begins_an_exchange(message: ModelMessage) -> bool:
    """Whether that message is a question: a request holding what a person said."""
    return isinstance(message, ModelRequest) and any(
        isinstance(part, UserPromptPart) for part in message.parts
    )


def _measured(message: ModelMessage) -> float:
    """That message's size in tokens, as ``CHARS_PER_TOKEN`` estimates it."""
    return len(ModelMessagesTypeAdapter.dump_json([message])) / CHARS_PER_TOKEN


def read_state(state: bytes | None) -> list[ModelMessage]:
    """The memory as the framework's messages; nothing for a conversation with none.

    A state this engine cannot read is a fault -- the platform hands an engine
    its own states only (``docs/specs/agents.md``) -- and is refused, which
    fails the turn, rather than read as nothing.
    """
    if state is None:
        return []
    try:
        return list(ModelMessagesTypeAdapter.validate_json(state))
    except (ValidationError, ValueError) as unreadable:
        raise InvalidValueError(
            "the conversation's memory is not one this engine wrote"
        ) from unreadable


def write_state(messages: Sequence[ModelMessage]) -> bytes:
    """The framework's messages as the memory: Pydantic AI's own encoding, as JSON."""
    return bytes(ModelMessagesTypeAdapter.dump_json(list(messages)))


def _events_of(event: object) -> list[Event]:
    """What one of the framework's events is to the port; nothing, for most."""
    if isinstance(event, PartStartEvent):
        if isinstance(event.part, TextPart) and event.part.content:
            return [TextDelta(text=event.part.content)]
        if isinstance(event.part, ThinkingPart) and event.part.content:
            return [ReasoningDelta(text=event.part.content)]
    elif isinstance(event, PartDeltaEvent):
        if isinstance(event.delta, TextPartDelta) and event.delta.content_delta:
            return [TextDelta(text=event.delta.content_delta)]
        if isinstance(event.delta, ThinkingPartDelta) and event.delta.content_delta:
            return [ReasoningDelta(text=event.delta.content_delta)]
    elif isinstance(event, FunctionToolCallEvent):
        return [
            ToolCall(
                call_id=event.part.tool_call_id,
                name=event.part.tool_name,
                arguments=event.part.args_as_dict(),
            )
        ]
    elif isinstance(event, FunctionToolResultEvent):
        part = event.part
        if isinstance(part, ToolReturnPart):
            return [
                ToolResult(
                    call_id=part.tool_call_id,
                    name=part.tool_name,
                    output=_bounded(part.model_response_str(wrap_if_error=False)),
                    is_error=part.outcome != "success",
                )
            ]
        # A retry prompt: the framework could not validate the call, and
        # tells the model so under the call's id.
        return [
            ToolResult(
                call_id=part.tool_call_id,
                name=part.tool_name or "",
                output=_bounded(part.model_response()),
                is_error=True,
            )
        ]
    return []


def _bounded(text: str) -> str:
    """A tool's output as the transcript can hold it: cut to a part's bound.

    What the model was sent is the framework's; what the transcript records of
    a result longer than one part may be is its beginning.
    """
    return text if len(text) <= MAX_PART_CHARS else text[:MAX_PART_CHARS]
