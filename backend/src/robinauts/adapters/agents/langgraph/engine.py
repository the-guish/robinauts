# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""One turn on LangChain: ``create_agent`` runs the loop, the adapter streams it.

The first implementation of the ``Agent`` port (``robinauts.ports.agents``),
and the only module in the platform that may name LangGraph or LangChain
(``docs/layout.md``, enforced by the import contracts in
``backend/pyproject.toml``). Everything about the framework stops here: what
crosses the port is a question and the conversation's memory in, and the
adapter's events out, and the **discard test** is that three places name this
sub-package and deleting it and its dependencies breaks those and nothing
else: its import in ``robinauts.app`` and its one entry in ``ENGINES``; the
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

**Nothing phones home** (``docs/specs/core.md``). LangSmith is off, explicitly,
at construction and whatever the environment says (``force_tracing_off``), and
a vendor's client is built from the configuration rather than from the
environment -- the endpoint, the key, the proxy and the key's header
(``ANTHROPIC_ENDPOINT``, ``clear_client_overrides``).

**And nothing is written down either.** The platform's logs never carry
conversation content: the vendor SDK's loggers that write request bodies --
which on ``ANTHROPIC_LOG``, and on any root logger turned up afterwards, put
every request's messages and system prompt on standard error -- are pinned at
``WARNING`` when the engine is built (``quiet_client_logging``).

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
from langchain.agents.middleware import AgentMiddleware, SummarizationMiddleware
from langchain_anthropic import ChatAnthropic
from langchain_anthropic.middleware import AnthropicPromptCachingMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    ToolMessage,
    messages_from_dict,
    messages_to_dict,
)
from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.sessions import StreamableHttpConnection

from robinauts.adapters.config_file import ProviderKeys, ToolServerSecrets, credential_header
from robinauts.domain import (
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
from robinauts.ports import Agent

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

TRACING_VARIABLES_REMOVED = ("LANGCHAIN_TRACING", "LANGCHAIN_HANDLER")
"""The two variables ``force_tracing_off`` takes out of the environment.

They ask for LangChain's **version 1** tracer, which no longer exists.
langchain-core does not ignore them: when either is set and v2 tracing is off
-- which is what this adapter makes sure of -- it raises, so every turn of a
process that inherited one would fail. Turning tracing off must not be a way
of breaking a deployment, and the only honest way to say "no v1 tracing
either" is to unset them.
"""

CLIENT_VARIABLES_REMOVED = ("ANTHROPIC_CUSTOM_HEADERS", "ANTHROPIC_LOG")
"""The variables ``clear_client_overrides`` takes out of the environment.

The two the client reads that **no argument can override**; the endpoint, the
key and the proxy are arguments, and an argument wins.

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

Belt as well as braces, and per call rather than process-wide: a header the
caller passes wins over anything merged in from the environment, so the key
that is sent is the configured one whether or not the variable above was
there to be removed.
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

ANTHROPIC_MODELS = frozenset({ProviderKind.ANTHROPIC, ProviderKind.ANTHROPIC_COMPATIBLE})


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
    not from the environment" (``ANTHROPIC_ENDPOINT``). An endpoint, a key and
    a proxy are arguments, and an argument wins; **headers are merged**, so
    the only way to say "and nothing else" is for the variable not to be
    there (``CLIENT_VARIABLES_REMOVED``).

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
    The Anthropic SDK -- which ``langchain-anthropic`` is built on -- reads
    ``ANTHROPIC_LOG`` at *import*, before anything here exists, and on
    ``debug`` puts its own logger and its HTTP client's at ``DEBUG`` and calls
    ``logging.basicConfig()``, which attaches a handler to the root logger if
    nothing else has. What the SDK's logger then writes for every call is
    "Request options: ..." with the request's ``json_data`` in it: the agent's
    system prompt and every message of the conversation, on standard error.

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
    """Where that provider is: the address the operator gave, or Anthropic's own.

    The two kinds this engine reaches are a **vendor** and a **protocol**
    (``LangGraphAgent.kinds``). ``anthropic`` is Anthropic, at
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
    held to ``LangGraphAgent.kinds`` at start-up. One that does -- a kind added to
    that set without a branch here, a caller that built a definition by hand --
    gets a refusal naming it, and never a turn sent to whichever endpoint
    happened to be nearest. The tests ask it of every kind the engine does not
    offer, so this is a branch that is exercised rather than merely written.
    """
    if provider.kind is ProviderKind.ANTHROPIC:
        return ANTHROPIC_ENDPOINT
    if provider.kind is ProviderKind.ANTHROPIC_COMPATIBLE and provider.base_url:
        return provider.base_url
    raise ConfigError(
        [
            f"model_providers.{provider.id}: this build of the LangGraph engine cannot"
            f" reach a {provider.kind.value} provider"
        ]
    )


def chat_model(model: ModelConfig, provider: ModelProviderConfig, key: str) -> BaseChatModel:
    """The chat model that model's configuration describes.

    The key is passed in and not read here: what may touch the environment is
    ``robinauts.adapters.config_file``, and what reaches this is one key for
    one call (``docs/specs/agents.md``).

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
    passed.
    """
    return ChatAnthropic(
        model=model.name,  # type: ignore[call-arg]  # `model_name`'s alias
        api_key=key,  # type: ignore[call-arg]  # `anthropic_api_key`'s alias
        base_url=endpoint_of(provider),  # type: ignore[call-arg]
        anthropic_proxy=None,
        default_headers={ANTHROPIC_KEY_HEADER: key},
        timeout=model.timeout_seconds,  # type: ignore[call-arg]
        max_retries=MAX_RETRIES,
        max_tokens=model.max_output_tokens or DEFAULT_ANTHROPIC_OUTPUT_TOKENS,
    )


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

    kinds: frozenset[ProviderKind] = ANTHROPIC_MODELS
    """The provider kinds this build of the engine has a client for.

    One client, two kinds: ``ChatAnthropic`` reaches Anthropic itself and any
    endpoint that speaks Anthropic's Messages API at an address the operator
    gives (``endpoint_of``). **That is how OpenRouter is reached in this
    build**: it serves the Messages API and takes the key in the same
    ``x-api-key`` header, so nothing about the client changes but where it
    sends the request.

    What is still not here is ``openai`` and ``openai-compatible``, which are
    reached through ``langchain-openai``, whose dependency tree does not pass
    the licence policy (``DEPENDENCIES.md``, "Known exclusions"). A provider
    whose client fails the gate is not offered until it passes
    (``docs/specs/agents.md``), so the configuration refuses the kind at
    start-up rather than the engine failing at the first turn. Adding it back
    is this set, a branch in ``chat_model`` and the dependency -- nothing
    else.

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
        cancelled run depends on (``robinauts.ports.agents``). It counts the
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
        # have been moved to another (``robinauts.ports.agents``).
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
    ]


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
