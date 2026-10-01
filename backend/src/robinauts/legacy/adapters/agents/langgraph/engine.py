# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""One turn on LangChain: ``create_agent`` runs the loop, the adapter streams it.

The first implementation of the ``Agent`` port (``robinauts.legacy.ports.agents``),
and the only module in the platform that may name LangGraph or LangChain
(``docs/layout.md``, enforced by the import contracts in
``backend/pyproject.toml``). Everything about the framework stops here: what
crosses the port is a question and the conversation's memory in, and the
adapter's events out, and the **discard test** is that three places name this
sub-package and deleting it and its dependencies breaks those and nothing
else: its import in ``robinauts.legacy.app`` and its one entry in ``ENGINES``; the
contract exceptions in ``backend/pyproject.toml`` that name the sub-package;
and this sub-package's own tests.

**The framework owns the loop, the context and the memory** (ADR 0005).
``create_agent`` builds the agent -- the model, the tools of the agent's MCP
servers through ``langchain-mcp-adapters``, the system prompt, and the
middleware that manages the context -- and runs the whole turn: the model
asks for a tool, the framework calls it, the result goes back, the model
answers, as many times as the turn needs. What this adapter does is translate
what it emits, exactly as the LangChain backend of `agent-framework-examples
<https://github.com/the-guish/agent-framework-examples>`_ does:

- the ``messages`` stream carries the model's chunks as they arrive: text
  becomes ``TextDelta`` and reasoning ``ReasoningDelta``, read through
  ``AIMessageChunk.content_blocks``, langchain-core's provider-neutral view
  -- and only the chunks of the **model node**, since the summarising
  middleware calls the model too and what it says is not the answer;
- the ``updates`` stream carries each node's finished messages, which is
  where a whole tool call and a tool result appear: the model node's
  ``AIMessage`` gives ``ToolCall`` once the arguments are known, and the tool
  node's ``ToolMessage`` gives ``ToolResult``, an error where the tool said so;
- the ``values`` stream carries the whole state after each node, and the last
  of it is the conversation as the framework leaves it, which ``Done`` hands
  back as the memory, written with langchain-core's own ``messages_to_dict``.
  The next turn is handed it back and ``messages_from_dict`` reads it: no
  translation, and whatever the framework keeps -- the vendor's signed
  thinking blocks, a summary the middleware wrote -- is kept.

**The memory is the framework's, in its format, and the platform never
reads it.** There is no checkpointer: the examples' LangChain backend keeps
the thread in one, and here the platform stores the same messages on the run
that produced them and hands them back, so that a fork of the transcript is a
fork of the memory and deleting a conversation deletes its memory
(``docs/specs/conversations.md``, "The model's memory").

**Context management is the middleware's.** ``SummarizationMiddleware``
summarises the older history and keeps the recent messages once the history
reaches a share of the model's window (``SUMMARIZE_AT``); the window is the
model's configured ``context_window``, or what the framework knows of the
model, or ``DEFAULT_CONTEXT_WINDOW`` for a model it has no table for. The
change is saved in the state, which is what the run stores.
``AnthropicPromptCachingMiddleware`` places the vendor's cache breakpoints.

**The tools are MCP servers, reached by the framework's own client.** Each
server the agent names becomes a Streamable HTTP connection with the
credential start-up read as its header (``mcp_connection``), and the tools
the servers list are bound for the turn, named ``<server id>_<tool>`` by the
client so that two servers offering one name never collide. The framework
runs them and, when a tool says it failed, tells the model rather than
failing the turn.

**Two protocols, four kinds** (``LangGraphAgent.kinds``). ``ChatAnthropic``
speaks Anthropic's Messages API, to Anthropic or to an ``anthropic-compatible``
endpoint; ``ChatOpenAI`` speaks OpenAI's **Chat Completions**, to OpenAI or to
an ``openai-compatible`` endpoint (``chat_model``). Everything past the client
-- the agent, the loop, the stream, the mapping, the memory -- is one path for
both.

**What the model is sent back is what the vendor takes back.** A ``thinking``
block without a signature -- what a model that signs nothing sends through an
Anthropic-compatible endpoint, OpenRouter's GPT for one -- is refused by the
Messages API when it comes back with a tool's results, so the middleware that
wraps every model call drops such blocks from the messages it sends
(``signed_blocks_only``); the memory keeps them, the model never sees them.
Pydantic AI does the same by itself.

**Nothing phones home** (``docs/specs/core.md``). LangSmith is off, explicitly,
at construction and whatever the environment says (``force_tracing_off``), and
a vendor's client is built from the configuration rather than from the
environment -- the endpoint, the key, the proxy and the key's header
(``ANTHROPIC_ENDPOINT``, ``OPENAI_ENDPOINT``, ``clear_client_overrides``).

**And nothing is written down either.** The platform's logs never carry
conversation content: the vendor SDKs' loggers that write request bodies --
which on ``ANTHROPIC_LOG`` or ``OPENAI_LOG``, and on any root logger turned up
afterwards, put every request's messages and system prompt on standard error
-- are pinned at ``WARNING`` when the engine is built (``quiet_client_logging``).

**Failure and cancellation** are the port's. Whatever the provider, a tool
server or the framework raises travels out of the generator as it is;
``CancelledError`` is never swallowed; and the ``finally`` closes the graph's
stream, which is what lets go of the model's stream, the HTTP response
underneath it and the tool sessions. A turn that raised hands back no memory:
the conversation resumes from the one it had.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import AsyncGenerator, Awaitable, Callable, Iterable, Mapping, Sequence
from contextlib import aclosing
from typing import Any

import langsmith
from langchain.agents import create_agent
from langchain.agents.middleware import (
    AgentMiddleware,
    ModelRequest,
    ModelResponse,
    SummarizationMiddleware,
    wrap_model_call,
)
from langchain_anthropic import ChatAnthropic
from langchain_anthropic.middleware import AnthropicPromptCachingMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    AnyMessage,
    BaseMessage,
    HumanMessage,
    ToolMessage,
    messages_from_dict,
    messages_to_dict,
)
from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.sessions import StreamableHttpConnection
from langchain_openai import ChatOpenAI
from openai import AsyncOpenAI, OpenAI

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

**A vendor's endpoint is not a thing to take from the environment.** Both the
Anthropic SDK and ``ChatAnthropic`` fall back to ``ANTHROPIC_BASE_URL`` /
``ANTHROPIC_API_URL`` when no base URL is passed, so one variable inherited
from a shell, a unit file or a container image would send every turn -- and
the operator's key with it -- to any host that variable named. The operator
says where a provider is in the configuration (``base_url``, and only for a
kind that names a protocol rather than a vendor), or it is this constant; there
is no third answer, and no way for the environment to be one
(``docs/specs/operations.md``). ``endpoint_of`` is where the choice between the
two is made, and it is the whole of it.
"""

OPENAI_ENDPOINT = "https://api.openai.com/v1"
"""Where OpenAI is, said here rather than left to the client to decide.

The same rule as ``ANTHROPIC_ENDPOINT``, for the other vendor: the OpenAI SDK
falls back to ``OPENAI_BASE_URL`` and ``ChatOpenAI`` to ``OPENAI_API_BASE``
before it, and either, inherited from a shell or an image, would send every
turn and the operator's key to the host it named. So the ``openai`` kind is
reached here and nowhere else, and it has no ``base_url`` to offer
(``domain.KINDS_WITH_BASE_URL``).

Spelt, unlike Anthropic's, **with** the ``/v1``: what OpenAI's client appends
to a base URL is ``/chat/completions`` alone, so the version is part of the
prefix -- which is also what an ``openai-compatible`` provider's ``base_url``
is (``endpoint_of``).
"""

ANTHROPIC_KINDS: frozenset[ProviderKind] = frozenset(
    {ProviderKind.ANTHROPIC, ProviderKind.ANTHROPIC_COMPATIBLE}
)
"""The kinds that speak Anthropic's Messages API, reached through ``ChatAnthropic``."""

OPENAI_KINDS: frozenset[ProviderKind] = frozenset(
    {ProviderKind.OPENAI, ProviderKind.OPENAI_COMPATIBLE}
)
"""The kinds that speak OpenAI's Chat Completions, reached through ``ChatOpenAI``.

Which client a turn is given is decided by these two sets and nothing else
(``chat_model``), and a kind in neither is refused rather than handed to
whichever client is nearer.
"""

COMPATIBLE_KINDS: frozenset[ProviderKind] = frozenset(
    {ProviderKind.ANTHROPIC_COMPATIBLE, ProviderKind.OPENAI_COMPATIBLE}
)
"""The two kinds whose endpoint is the operator's ``base_url`` (``endpoint_of``).

Written out here rather than read off ``domain.KINDS_WITH_BASE_URL``, which is
the same two today: that set says which kinds the configuration gives an
address, and this one which addresses this engine has a client to send to. A
protocol kind added to the first tomorrow must not be sent anywhere by the
second before somebody has written its client.
"""

CEILING_FIELDS: Mapping[ProviderKind, str] = {
    ProviderKind.OPENAI: "max_completion_tokens",
    ProviderKind.OPENAI_COMPATIBLE: "max_tokens",
}
"""The field a configured output ceiling is sent in, per OpenAI-protocol kind.

OpenAI's current models take ``max_completion_tokens`` and refuse the older
``max_tokens`` on its reasoning models; OpenRouter and many older compatible
servers take ``max_tokens`` only -- Pydantic AI's own OpenRouter profile says
so. A compatible endpoint is anybody's, so it is sent the field every one of
them knows, and OpenAI itself the field it asks for. The other engine sends
the same (``docs/specs/agents.md``).
"""

TRACING_VARIABLES_REMOVED = ("LANGCHAIN_TRACING", "LANGCHAIN_HANDLER")
"""The two variables ``force_tracing_off`` takes out of the environment.

They ask for LangChain's **version 1** tracer, which no longer exists.
langchain-core does not ignore them: when either is set and v2 tracing is off
-- which is what this adapter makes sure of -- it raises, so every turn of a
process that inherited one would fail. Turning tracing off must not be a way
of breaking a deployment, and the only honest way to say "no v1 tracing
either" is to unset them.
"""

CLIENT_VARIABLES_REMOVED = (
    "ANTHROPIC_CUSTOM_HEADERS",
    "ANTHROPIC_LOG",
    "OPENAI_CUSTOM_HEADERS",
    "OPENAI_LOG",
    "OPENAI_ORG_ID",
    "OPENAI_ORGANIZATION",
    "OPENAI_PROJECT_ID",
    "OPENAI_ADMIN_KEY",
)
"""The variables ``clear_client_overrides`` takes out of the environment.

The ones the two clients read that **no argument can override**; the endpoint,
the key and the proxy are arguments, and an argument wins.

``ANTHROPIC_CUSTOM_HEADERS`` because the SDK *merges* what it holds into
whatever the caller passed, so one line of it replaces the ``x-api-key``
header outright and a turn is spent on somebody else's key, or adds headers
nobody configured. An argument cannot say "and nothing else", so the variable
goes.

``ANTHROPIC_LOG`` because it is read at **import**, before any argument
exists: the Anthropic SDK under ``ChatAnthropic`` calls its own
``setup_logging()`` at the bottom of its ``__init__``, and ``debug`` or
``info`` there puts the ``anthropic`` and ``httpx2`` loggers at that level for
the whole process. What the first of them then writes is every request's
options, ``json_data`` included -- which is the system prompt and every
message of the conversation, in a log. Removing the variable is only half the
answer, since by construction time the import has already happened:
``quiet_client_logging`` is the other half, and it is the half that works.

The OpenAI SDK under ``ChatOpenAI`` has the same two, and four more of its
own. ``OPENAI_CUSTOM_HEADERS`` is merged exactly as Anthropic's is -- the key
travels in ``Authorization`` there, and an ``Authorization`` line in the
variable is dropped only when the caller passed one, which ``chat_model``
always does (``OPENAI_KEY_HEADER``); anything else in it would be sent on
every turn. ``OPENAI_LOG`` is read at import, as ``ANTHROPIC_LOG`` is, and on
``debug`` puts the ``openai`` logger at ``DEBUG`` and calls
``logging.basicConfig()``; this version of the SDK writes no request body there,
but the promise is not left to rest on what one version happens to log.
``OPENAI_ORG_ID`` and ``OPENAI_PROJECT_ID`` are read when the client is built
and not given, and ``None`` -- the only way to say "none" -- *is* not given,
so no argument can keep an ``OpenAI-Organization`` or ``OpenAI-Project``
header nobody configured off a turn; ``ChatOpenAI`` reads the first as
``OPENAI_ORGANIZATION`` too, with ``or``, so that an empty argument falls
through to it. And ``OPENAI_ADMIN_KEY`` is a second credential the client
picks up whenever it is not passed one, and holds for the life of the client:
it is not what a chat request is signed with, and it is still a key the
operator did not configure for this provider, in an object built for a turn.

Removed once, at construction, and never per turn: a process-wide edit made
while turns are running would be one turn changing another's environment.

What is **not** here, because an argument does say it or because nothing on a
turn's path reads it: ``OPENAI_API_KEY``, ``OPENAI_BASE_URL`` and
``OPENAI_API_BASE`` (the key and the endpoint are passed), ``OPENAI_PROXY``
(passed as ``None``), ``LC_OUTPUT_VERSION``,
``LANGCHAIN_OPENAI_STREAM_CHUNK_TIMEOUT_S`` and the
``LANGCHAIN_OPENAI_TCP_*`` socket settings (each passed, ``chat_model``),
``OPENAI_WEBHOOK_SECRET`` (read into a client, and used only to verify a
webhook, which the platform never receives), and the Azure and Bedrock
variables (``AZURE_OPENAI_*``, ``OPENAI_API_TYPE``, ``OPENAI_API_VERSION``,
``AWS_*``), which only the SDK's module-level client and its Azure and Bedrock
clients read, and none of those is ever built.
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
body at ``DEBUG`` -- its own comment says bodies "can contain private data" --
but ``OPENAI_LOG`` still turns the pair on for the whole process, and a pin is
cheaper than a promise that holds until an upgrade.

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

Belt as well as braces, and per call rather than process-wide: a header the
caller passes wins over anything merged in from the environment, so the key
that is sent is the configured one whether or not the variable above was
there to be removed.
"""

OPENAI_KEY_HEADER = "Authorization"
"""The header OpenAI's key travels in, as ``Bearer <key>``, pinned by ``chat_model``.

The same belt as ``ANTHROPIC_KEY_HEADER``, and a little more than a belt: the
OpenAI SDK drops an ``Authorization`` line of ``OPENAI_CUSTOM_HEADERS`` only
when the caller passed an ``Authorization`` header of its own, so passing one is
what makes the configured key the only one that can be sent.
"""

OUTPUT_VERSION = "v0"
"""The shape langchain-core stores a chat model's answer in: the provider's own.

``BaseChatModel.output_version`` defaults to ``LC_OUTPUT_VERSION`` from the
environment, and ``v1`` there rewrites every answer's content into
langchain-core's standard blocks. The memory is written in whatever shape the
answers arrive in (``write_state``), and a memory has to read back under the
next process as it was written under this one, so the shape is an argument
and the variable is not consulted. What this adapter reads of a chunk it reads
through ``content_blocks``, which is the same view under either shape.
"""

MAX_RETRIES = 0
"""How many times a failed model call is retried by the client: not at all.

The client's own retries are invisible to everything above -- they would
lengthen a turn silently and could send the same prompt twice after a timeout
the provider had already accepted. A turn is bounded by the application
(``robinauts.legacy.application.Turns``), a failure is reported by raising, and
retrying is sending the message again (``docs/specs/runs.md``).
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
a number would be a worse one here than it is for Anthropic: on OpenAI's
reasoning models the ceiling covers the **reasoning** tokens as well as the
answer, so a ceiling picked to bound an answer's length can be spent entirely
on thinking and leave an empty answer. An operator who wants a bound writes
``max_output_tokens``, and it is sent in the field the kind takes
(``CEILING_FIELDS``). The other engine sends the same (``docs/specs/agents.md``).
"""

MAX_TOOL_ROUNDS = 25
"""How many times one turn may go back to the model with tool results.

A bound on the loop, so that a model that keeps calling tools cannot run a
turn for ever on the operator's account; the framework's ``recursion_limit``
is set from it (``RECURSION_LIMIT``), and a turn that reaches it fails with
what the framework says. Twenty-five rounds is far past what a turn that is
getting somewhere needs and far short of what a turn that is not would spend.
"""

STEPS_PER_ROUND = 3
"""How many steps of the framework's graph one tool round is.

The summarising middleware's step before the model, the model's, and the
tools' (``middleware``): the caching middleware wraps the model's call and
adds none. A middleware added with a step of its own changes this number,
and the test of the bound is what says so.
"""

RECURSION_LIMIT = STEPS_PER_ROUND * MAX_TOOL_ROUNDS + STEPS_PER_ROUND - 1
"""The framework's bound on a turn: the rounds, and the last answer's steps without the tools'."""

DEFAULT_CONTEXT_WINDOW = 200_000
"""The context window assumed for a model nobody said the window of.

For a model the framework has no table for -- a gateway's model id -- and no
``context_window`` in the configuration. Haiku 4.5's, and conservative for a
bigger model: summarising too early costs a summary, summarising too late
costs a refused request.
"""

SUMMARIZE_AT = 0.8
"""The share of the window at which the older history is summarised."""

KEEP_MESSAGES = 20
"""How many recent messages a summary leaves as they were."""

MODEL_NODE = "model"
"""The node of ``create_agent``'s graph that calls the model; its chunks are the answer."""

TOOLS_NODE = "tools"
"""The node that runs the tools; its messages are the results."""


ChatModelFactory = Callable[[ModelConfig, ModelProviderConfig, str], BaseChatModel]
"""How a chat model is built: the model, its provider, and the provider's key.

Injectable so that the tests run the engine over a chat model they script --
the graph, the streaming, the mapping and the releasing are then exercised for
real with no network and no key (``docs/layout.md``, "Testing strategy").
"""

ToolsFactory = Callable[
    [Sequence[ToolServerConfig], ToolServerSecrets], Awaitable[Sequence[BaseTool]]
]
"""How the tools of the agent's servers are listed for a turn.

Injectable for the same reason: the tests hand the engine plain functions as
tools, as the examples do, and the framework runs them exactly as it runs a
server's.
"""


def force_tracing_off() -> None:
    """Turn LangSmith off for this process, whatever the environment says.

    **Nothing phones home** (``docs/specs/core.md``, ``docs/specs/agents.md``).
    langchain-core decides whether to trace by asking langsmith
    (``langsmith.utils.tracing_is_enabled``), which answers from, in order: the
    tracing context, a run already in flight, a process-wide setting, and only
    then ``LANGSMITH_TRACING`` / ``LANGCHAIN_TRACING_V2`` in the environment.
    ``langsmith.configure(enabled=False)`` sets the process-wide setting *and*
    the context, so the environment is never reached and no ``LangChainTracer``
    is ever attached to a run -- which is the only thing that would open a
    client and send a conversation to a third party.

    It is deliberately **not** enough to pass no callbacks: langchain-core adds
    the tracer itself when it believes tracing is on, whatever a caller's
    ``config`` says. And it is deliberately process-wide: a stale export in a
    unit file must not turn tracing on for somebody else's runnable either.

    **It also unsets two variables**, which is the one thing here that reaches
    out of this adapter and into the process. An adapter may touch the
    environment -- it is the layer that may -- and this is why it must:
    ``LANGCHAIN_TRACING`` and ``LANGCHAIN_HANDLER`` ask for the version 1
    tracer, and langchain-core **raises** when one of them is set and v2
    tracing is off. Left alone, a deployment that inherited either would fail
    every turn as a *result* of tracing being turned off, which would make
    "nothing phones home" a way of breaking a server. Unsetting them says the
    same thing the switch above says, in the only words that half of
    langchain-core reads (``TRACING_VARIABLES_REMOVED``).
    """
    langsmith.configure(enabled=False)
    for name in TRACING_VARIABLES_REMOVED:
        os.environ.pop(name, None)


def clear_client_overrides() -> None:
    """Take out of the environment what no argument can override.

    The other half of "a vendor's client is built from the configuration and
    not from the environment" (``ANTHROPIC_ENDPOINT``, ``OPENAI_ENDPOINT``). An
    endpoint, a key and a proxy are arguments, and an argument wins; **headers
    are merged**, and an organisation, a project and an admin key are read
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
    The Anthropic SDK -- which ``langchain-anthropic`` is built on -- reads
    ``ANTHROPIC_LOG`` at *import*, before anything here exists, and on
    ``debug`` puts its own logger and its HTTP client's at ``DEBUG`` and calls
    ``logging.basicConfig()``, which attaches a handler to the root logger if
    nothing else has. What the SDK's logger then writes for every call is
    "Request options: ..." with the request's ``json_data`` in it: the agent's
    system prompt and every message of the conversation, on standard error.
    The OpenAI SDK under ``langchain-openai`` reads ``OPENAI_LOG`` the same
    way, and is pinned the same way.

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

    Spelt out again here rather than imported from the Pydantic AI adapter:
    the two adapters do not import each other (``docs/layout.md``), they reach
    the same SDK by different routes, and deleting either must leave the other
    whole.
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
    **protocols** (``LangGraphAgent.kinds``). ``anthropic`` is Anthropic, at
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
    held to ``LangGraphAgent.kinds`` at start-up. One that does -- a kind added
    to the platform's vocabulary without a branch here, a caller that built a
    definition by hand -- gets a refusal naming it, and never a turn sent to
    whichever endpoint happened to be nearest. Every kind there is today has a
    branch, so the tests reach this one with a kind of their own, which is what
    keeps it a branch that is exercised rather than merely written.
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
            f"model_providers.{provider.id}: this build of the LangGraph engine cannot"
            f" reach a {provider.kind.value} provider"
        ]
    )


def chat_model(model: ModelConfig, provider: ModelProviderConfig, key: str) -> BaseChatModel:
    """The chat model that model's configuration describes.

    The key is passed in and not read here: what may touch the environment is
    ``robinauts.legacy.adapters.config_file``, and what reaches this is one key for
    one call (``docs/specs/agents.md``).

    **The provider's kind chooses the client, and nothing else does**: the two
    Anthropic kinds get ``ChatAnthropic``, described below, and the two OpenAI
    kinds ``ChatOpenAI`` (``_openai_chat_model``). A kind in neither set is
    refused, naming the provider.

    **Everything the client would otherwise take from the environment is
    passed.** The key, so that ``ANTHROPIC_API_KEY`` is never consulted and
    the key a turn spends is the one the operator configured for that
    provider; the endpoint (``endpoint_of``), so that ``ANTHROPIC_BASE_URL`` /
    ``ANTHROPIC_API_URL`` cannot redirect a turn and a key to another host --
    the configuration is the only thing that decides where a turn goes; and
    the proxy, as ``None``, so that
    ``ANTHROPIC_PROXY`` -- a variable only this one client would obey -- is
    not a second way to do the same thing. An operator who needs a proxy sets
    ``HTTPS_PROXY``, which is theirs and is how every other outbound call of
    this process is proxied. An explicit key and endpoint also keep the
    LangSmith gateway out of it: langchain-core reaches for it only when
    neither was given.

    Headers are the one thing an argument cannot settle on its own, because
    the SDK **merges** ``ANTHROPIC_CUSTOM_HEADERS`` into what the caller
    passed: the key is therefore pinned as a header too
    (``ANTHROPIC_KEY_HEADER``), where a caller's value wins, and the variable
    itself is taken out of the environment when the engine is built
    (``clear_client_overrides``). What is deliberately left alone: the SDK's
    credential auto-discovery, which is not consulted at all once a key is
    passed. The shape the answer is stored in is an argument too
    (``OUTPUT_VERSION``), so that ``LC_OUTPUT_VERSION`` cannot change what the
    memory is written in.
    """
    if provider.kind in OPENAI_KINDS:
        return _openai_chat_model(model, provider, key)
    if provider.kind not in ANTHROPIC_KINDS:
        raise _unreachable(provider)
    return ChatAnthropic(
        model=model.name,  # type: ignore[call-arg]  # `model_name`'s alias
        api_key=key,  # type: ignore[call-arg]  # `anthropic_api_key`'s alias
        base_url=endpoint_of(provider),  # type: ignore[call-arg]
        anthropic_proxy=None,
        default_headers={ANTHROPIC_KEY_HEADER: key},
        timeout=model.timeout_seconds,  # type: ignore[call-arg]
        max_retries=MAX_RETRIES,
        max_tokens=model.max_output_tokens or DEFAULT_ANTHROPIC_OUTPUT_TOKENS,
        output_version=OUTPUT_VERSION,
    )


def _openai_chat_model(model: ModelConfig, provider: ModelProviderConfig, key: str) -> ChatOpenAI:
    """``ChatOpenAI`` over Chat Completions, with clients this adapter built itself.

    **The protocol is Chat Completions, for both kinds**, and never the
    Responses API: ``use_responses_api`` is ``False`` rather than left to
    ``ChatOpenAI``, which otherwise switches to Responses by itself for some
    model names and some arguments. An ``openai-compatible`` endpoint -- a
    gateway, vLLM, OpenRouter -- speaks Chat Completions, one protocol for both
    kinds keeps the two kinds and the two engines symmetric, and Pydantic AI's
    engine reaches the same endpoints through its own Chat Completions model
    (``docs/specs/agents.md``). The Responses API is a decision of its own.

    **The vendor's clients are built here and handed over**, as the other
    engine builds its own: an ``AsyncOpenAI`` for the turn, and an ``OpenAI``
    beside it only because ``ChatOpenAI`` builds a synchronous client of its
    own when it is not given one, and a client it built would be one this
    adapter had not pinned. Built here, each gets exactly what the
    configuration says -- the key, the endpoint (``endpoint_of``), the timeout,
    no retries (``MAX_RETRIES``) and the key pinned as its header
    (``OPENAI_KEY_HEADER``) -- and none of what ``ChatOpenAI`` would otherwise
    add on the way: an HTTP client **shared by every turn of the process** from
    a cache of its own, with TCP socket options read from
    ``LANGCHAIN_OPENAI_TCP_*``. What the SDK itself reads when an argument is
    ``None`` -- the organisation, the project, the admin key, the headers -- is
    out of the environment by the time a turn runs (``clear_client_overrides``).

    **And every environment fallback ``ChatOpenAI`` has is an argument.** The
    key and the endpoint, so that ``OPENAI_API_KEY``, ``OPENAI_API_BASE`` and
    the LangSmith gateway are never consulted; the proxy, as ``None``, so that
    ``OPENAI_PROXY`` -- a proxy only this client would obey -- is not a second
    ``HTTPS_PROXY``; ``stream_usage``, which it otherwise turns on or off
    according to whether ``OPENAI_BASE_URL`` is set, on, so that the stream's
    usage is what the summarising middleware measures by;
    ``stream_chunk_timeout``, off, because a turn's timeouts are the
    platform's -- the model call's (``timeout_seconds``) and the turn's -- and a
    third, set by ``LANGCHAIN_OPENAI_STREAM_CHUNK_TIMEOUT_S`` and found on
    neither the other client nor the other engine, would be a failure nobody
    configured; the socket options, as none; and the output shape
    (``OUTPUT_VERSION``).

    **The ceiling is sent only when the model's configuration has one**
    (``DEFAULT_OPENAI_OUTPUT_TOKENS``), and in the field the kind takes
    (``CEILING_FIELDS``, ``_ChatCompletions``).
    """
    endpoint = endpoint_of(provider)
    headers = {OPENAI_KEY_HEADER: f"Bearer {key}"}
    asynchronous = AsyncOpenAI(
        api_key=key,
        base_url=endpoint,
        timeout=model.timeout_seconds,
        max_retries=MAX_RETRIES,
        default_headers=headers,
    )
    synchronous = OpenAI(
        api_key=key,
        base_url=endpoint,
        timeout=model.timeout_seconds,
        max_retries=MAX_RETRIES,
        default_headers=headers,
    )
    return _ChatCompletions(
        ceiling_field=CEILING_FIELDS[provider.kind],
        model=model.name,
        api_key=key,  # type: ignore[arg-type]  # a str becomes the SecretStr
        base_url=endpoint,
        root_async_client=asynchronous,
        async_client=asynchronous.chat.completions,
        root_client=synchronous,
        client=synchronous.chat.completions,
        openai_proxy=None,
        timeout=model.timeout_seconds,
        max_retries=MAX_RETRIES,
        max_tokens=model.max_output_tokens or DEFAULT_OPENAI_OUTPUT_TOKENS,
        stream_usage=True,
        use_responses_api=False,
        output_version=OUTPUT_VERSION,
        stream_chunk_timeout=None,
        http_socket_options=(),
    )


class _ChatCompletions(ChatOpenAI):
    """``ChatOpenAI``, sending the ceiling in the field the kind takes.

    ``ChatOpenAI`` always renames a configured ceiling to
    ``max_completion_tokens``, which OpenAI's current models take and many
    compatible servers do not (``CEILING_FIELDS``); the payload is corrected
    after ``ChatOpenAI`` has built it and before the SDK sends it, which is the
    one place both paths of the client go through.
    """

    ceiling_field: str = "max_completion_tokens"
    """Which field the ceiling is sent in: ``max_completion_tokens`` or ``max_tokens``."""

    def _get_request_payload(
        self, input_: Any, *, stop: list[str] | None = None, **kwargs: Any
    ) -> dict[str, Any]:
        payload: dict[str, Any] = super()._get_request_payload(input_, stop=stop, **kwargs)
        for field in ("max_completion_tokens", "max_tokens"):
            if field != self.ceiling_field and field in payload:
                payload[self.ceiling_field] = payload.pop(field)
        return payload


def mcp_connection(
    server: ToolServerConfig, secrets: ToolServerSecrets
) -> StreamableHttpConnection:
    """That server as the framework's client connects to it, the credential a header.

    The endpoint is the configured one and nothing else, the credential is the
    one start-up read for the server (``credential_header``: ``Bearer``,
    ``Basic``, or no header at all under ``auth = "none"``), and the server's
    ``timeout_seconds`` bounds every request to it and the wait for its
    stream. ``HTTPS_PROXY`` is obeyed by the client's ``httpx`` as by every
    other outbound call of the process (``docs/specs/agents.md``, "Tools").
    """
    return {
        "transport": "streamable_http",
        "url": server.url,
        "headers": credential_header(server, secrets),
        "timeout": server.timeout_seconds,
        "sse_read_timeout": server.timeout_seconds,
    }


async def mcp_tools(
    servers: Sequence[ToolServerConfig], secrets: ToolServerSecrets
) -> Sequence[BaseTool]:
    """The tools those servers list, as the framework's tools, named under the server's id.

    ``langchain-mcp-adapters`` lists each server's tools over a session of its
    own and hands back a ``BaseTool`` per tool that opens a session per call;
    ``tool_name_prefix`` names each ``<server id>_<tool>`` so that two servers
    offering ``search`` never collide, and ``handle_tool_errors`` turns a
    result the server marked as an error into a message the model reads rather
    than a failure of the turn (``docs/specs/runs.md``, "Tools"). A server that
    cannot be reached raises, which fails the turn.
    """
    if not servers:
        return ()
    client = MultiServerMCPClient(
        {server.id: mcp_connection(server, secrets) for server in servers},
        tool_name_prefix=True,
        handle_tool_errors=True,
    )
    return await client.get_tools()


class LangGraphAgent(Agent):
    """The LangChain engine: one turn, one agent, built and thrown away."""

    kinds: frozenset[ProviderKind] = ANTHROPIC_KINDS | OPENAI_KINDS
    """The provider kinds this build of the engine has a client for: all four.

    Two clients, two kinds each. ``ChatAnthropic`` reaches Anthropic itself and
    any endpoint that speaks Anthropic's Messages API at an address the
    operator gives; ``ChatOpenAI`` reaches OpenAI itself and any endpoint that
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
        chat_model_for: ChatModelFactory = chat_model,
        tools_for: ToolsFactory = mcp_tools,
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
        self._chat_model_for = chat_model_for
        self._tools_for = tools_for
        self._open = 0

    @property
    def held(self) -> int:
        """How many turns of this engine still hold a stream open.

        Zero once every turn has ended or been closed, which is the promise a
        cancelled run depends on (``robinauts.legacy.ports.agents``). It counts the
        engine's own graph streams; the model's stream and the tool sessions
        live inside one and go with it.

        **One engine, one count, however many turns it is running.** A
        deployment shares one ``LangGraphAgent`` between every conversation,
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
        model, listing the tools, building the agent, opening the stream --
        happens inside the generator, so that a provider that refuses a key is
        a failure of the turn, reported by raising where the caller is
        iterating, and not an exception thrown at whoever asked for the stream.
        """
        return self._turn(agent, prompt, model, state)

    async def _turn(
        self, agent: AgentDefinition, prompt: str, model_id: str, state: bytes | None
    ) -> AsyncGenerator[Event, None]:
        # The run's model, never the agent's default: the conversation may
        # have been moved to another (``robinauts.legacy.ports.agents``).
        model = self._models.model_by_id(model_id)
        provider = self._models.provider_for(model)
        chat = self._chat_model_for(model, provider, self._keys.key_for(provider.id))
        servers = [self._models.tool_servers[server_id] for server_id in agent.tools]
        history = read_state(state)
        answer: AIMessage | None = None
        final: list[BaseMessage] = []
        self._open += 1
        try:
            tools = await self._tools_for(servers, self._tool_secrets)
            graph = create_agent(
                chat,
                tools=list(tools),
                system_prompt=agent.system_prompt or None,
                middleware=middleware(chat, model),
            )
            stream = graph.astream(
                {"messages": [*history, HumanMessage(prompt)]},
                stream_mode=["messages", "updates", "values"],
                # A fresh configuration each turn, carrying no callbacks: a
                # turn inherits nothing from whatever context it happens to
                # run in. What keeps a tracer away is `force_tracing_off`;
                # this keeps everything else away.
                config={"callbacks": [], "recursion_limit": RECURSION_LIMIT},
            )
            async with aclosing(stream):
                async for mode, payload in stream:
                    if mode == "messages":
                        chunk, metadata = payload
                        if metadata.get("langgraph_node") == MODEL_NODE:
                            for event in _deltas_of(chunk):
                                yield event
                    elif mode == "updates":
                        for message in _messages_of(payload, MODEL_NODE):
                            if isinstance(message, AIMessage):
                                answer = message
                                for call in message.tool_calls:
                                    yield ToolCall(
                                        call_id=str(call.get("id") or ""),
                                        name=call["name"],
                                        arguments=dict(call.get("args") or {}),
                                    )
                        for message in _messages_of(payload, TOOLS_NODE):
                            if isinstance(message, ToolMessage):
                                yield ToolResult(
                                    call_id=message.tool_call_id,
                                    name=message.name or "",
                                    output=_bounded(message.text),
                                    is_error=message.status == "error",
                                )
                    else:
                        final = list(payload.get("messages", ()))
            yield Done(text=answer.text if answer is not None else "", state=write_state(final))
        finally:
            # Reached when the turn ends, when it raises, and when the
            # iteration is closed -- which is what a cancellation does.
            self._open -= 1


def middleware(chat: BaseChatModel, model: ModelConfig) -> list[AgentMiddleware[Any, Any]]:
    """What manages the context of a turn: summarisation and the vendor's cache.

    The window the summarisation measures against is the model's configured
    ``context_window``, then what the framework knows of the model
    (``profile["max_input_tokens"]``), then ``DEFAULT_CONTEXT_WINDOW``. The
    trigger is written as a token count rather than a fraction, because the
    fraction form needs a profile the framework has not got for a gateway's
    model ids (``docs/specs/agents.md``, "A turn").
    """
    return [
        SummarizationMiddleware(
            chat,
            trigger=("tokens", int(context_window(chat, model) * SUMMARIZE_AT)),
            keep=("messages", KEEP_MESSAGES),
        ),
        # Warns, by default, on a model that is not the vendor's; a scripted
        # model in the tests is not, and the warning would be a failure there.
        AnthropicPromptCachingMiddleware(unsupported_model_behavior="ignore"),
        signed_blocks_only,
    ]


@wrap_model_call
async def signed_blocks_only(
    request: ModelRequest, handler: Callable[[ModelRequest], Awaitable[ModelResponse]]
) -> ModelResponse:
    """Send the model the thinking blocks it takes back, and no other.

    A ``thinking`` block with a signature, or a ``redacted_thinking`` block
    with its data, is what the Messages API requires back when a tool's
    results go back; a ``thinking`` block **without** one is what a model that
    signs nothing sends through an Anthropic-compatible endpoint (OpenRouter's
    GPT, for one), and the API refuses the whole request over it. The memory
    keeps every block as it arrived -- it is the framework's own history --
    and what is sent is the same messages less those blocks, for this call
    only. A model reached over OpenAI's protocol has no such blocks, and its
    messages pass through untouched.
    """
    sent = [_signed_blocks_of(message) for message in request.messages]
    return await handler(request.override(messages=sent))


def _signed_blocks_of(message: AnyMessage) -> AnyMessage:
    """That message with every thinking block the vendor would refuse left out."""
    if not isinstance(message, AIMessage) or not isinstance(message.content, list):
        return message
    kept = [block for block in message.content if not _unsigned_thinking(block)]
    if len(kept) == len(message.content):
        return message
    return message.model_copy(update={"content": kept})


def _unsigned_thinking(block: object) -> bool:
    """Whether a block is thinking the vendor would not take back."""
    if not isinstance(block, Mapping):
        return False
    if block.get("type") == "redacted_thinking":
        return not (isinstance(block.get("data"), str) and block["data"])
    return block.get("type") == "thinking" and not (
        isinstance(block.get("signature"), str) and block["signature"]
    )


def context_window(chat: BaseChatModel, model: ModelConfig) -> int:
    """The window the context is managed against, in tokens."""
    if model.context_window is not None:
        return model.context_window
    profile = chat.profile or {}
    known = profile.get("max_input_tokens")
    if isinstance(known, int) and known > 0:
        return known
    return DEFAULT_CONTEXT_WINDOW


def read_state(state: bytes | None) -> list[BaseMessage]:
    """The memory as the framework's messages; nothing for a conversation with none.

    A state this engine cannot read is a fault -- the platform hands an engine
    its own states only (``docs/specs/agents.md``) -- and is refused, which
    fails the turn, rather than read as nothing.
    """
    if state is None:
        return []
    try:
        return messages_from_dict(json.loads(state))
    except (ValueError, TypeError, KeyError) as unreadable:
        raise InvalidValueError(
            "the conversation's memory is not one this engine wrote"
        ) from unreadable


def write_state(messages: Sequence[BaseMessage]) -> bytes:
    """The framework's messages as the memory: langchain-core's own encoding, as JSON."""
    return json.dumps(messages_to_dict(messages)).encode("utf-8")


def _deltas_of(chunk: object) -> Iterable[Event]:
    """The text and the reasoning one streamed chunk carries, in order."""
    if not isinstance(chunk, AIMessageChunk):
        # A model that does not stream: LangGraph passes the whole
        # ``AIMessage`` through here unchanged, and the answer arrives with
        # the node's update instead.
        return
    for block in chunk.content_blocks:
        kind = block.get("type")
        if kind == "text" and block.get("text"):
            yield TextDelta(text=str(block["text"]))
        elif kind == "reasoning" and block.get("reasoning"):
            yield ReasoningDelta(text=str(block["reasoning"]))


def _messages_of(payload: object, node: str) -> list[BaseMessage]:
    """The messages that node's update carries, if the update is that node's."""
    if not isinstance(payload, Mapping):  # pragma: no cover -- LangGraph yields these
        return []
    update = payload.get(node)
    if not isinstance(update, Mapping):
        return []
    messages = update.get("messages") or []
    return [message for message in messages if isinstance(message, BaseMessage)]


def _bounded(text: str) -> str:
    """A tool's output as the transcript can hold it: cut to a part's bound.

    What the model was sent is the framework's; what the transcript records of
    a result longer than one part may be is its beginning.
    """
    return text if len(text) <= MAX_PART_CHARS else text[:MAX_PART_CHARS]
