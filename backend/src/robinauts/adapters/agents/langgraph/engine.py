# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""One turn on LangGraph: a graph compiled per turn, streamed, translated.

The first implementation of the ``Agent`` port (``robinauts.ports.agents``),
and the only module in the platform that may name LangGraph or LangChain
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

**Stateless per turn** (ADR 0002). The graph is compiled for the turn, with
**no checkpointer**: the conversation record is the whole of the state and the
history handed in is where the turn starts from. Nothing is remembered between
calls, which is what lets the next turn of the same conversation run on the
other engine.

**A real graph, not a shortcut.** One node, which streams the chat model, and
two edges -- and **no ``ToolNode``**: the platform owns the tool loop
(``docs/specs/agents.md``, "Tools"). The tools a run has are bound to the
model (``bind_tools``), a call the model makes is yielded as events and the
turn ends there, waiting; the application runs the call, appends the result
and starts the next turn from the stored history, which this engine then sees
as a path ending in a tool message.

**The mapping**, which is the whole of the translation:

- LangGraph's ``messages`` stream carries what the model produced, chunk by
  chunk. The first chunk with anything in it opens the answer
  (``AnswerStarted``); text becomes ``AnswerTextDelta``, reasoning becomes
  ``AnswerReasoningDelta``, and a tool call becomes ``ToolCallStarted`` with
  its id and name, ``ToolCallArgumentsDelta`` for the JSON the model writes,
  and ``ToolCallCompleted`` with the platform's part for it;
- the node's own update carries the whole message, which is what an answer
  **that never streamed** completes with, where a call still open when the
  answer ends and that streamed no arguments takes them from, and where the
  vendor's signed thinking blocks are read off (``_Answer.complete``). A
  call closed by the next call's start with nothing streamed completes with
  no arguments, which is what this client's stream gives such a call anyway;
- **what was streamed is what is kept** (``docs/specs/agents.md``): an answer
  that yielded text deltas completes with exactly those deltas joined, never
  with whatever the framework made of the final message, and a call's
  arguments are what its deltas parse to;
- **reasoning is never in the completed parts.** ``domain.kept_parts`` would
  drop a ``ReasoningPart`` anyway; putting the model's thinking in the answer's
  *text* is the mistake that would survive that, so the text part is built
  from the text deltas alone. What **is** kept of the thinking is the vendor's
  signed blocks, in ``AnswerCompleted.extras`` under ``anthropic``, unread
  (``docs/specs/conversations.md``, "Reasoning"): the vendor requires them back
  with a tool call's results, and this adapter replays them -- to the model
  that made them, since they are bound to it -- when it translates the
  history (``_assistant``);
- **an answer that asked for tools ends the turn waiting** (``WaitingOnTools``)
  and the engine executes none of them.

Chunks are read through ``AIMessageChunk.content_blocks``, langchain-core's
own provider-neutral view of a message's content, for text and reasoning, and
through ``tool_call_chunks`` for calls, which is where every provider client
puts a streamed call.

**Its context policy is everything** (ADR 0004): the whole visible path goes
to the model, in order, with the system prompt in front. Trimming to a token
budget and cache breakpoints are this adapter's to add, and are not added yet.

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

**Failure and cancellation** are the port's. Whatever the provider raises
travels out of the generator as it is; ``CancelledError`` is never swallowed;
and the ``finally`` closes the graph's stream, which is what lets go of the
model's stream, the HTTP response underneath it and the connection.

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

import json
import logging
import os
from collections.abc import AsyncGenerator, Callable, Mapping, Sequence
from contextlib import aclosing
from typing import Any

import langsmith
from langchain_anthropic import ChatAnthropic
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    ToolMessage,
)
from langchain_core.messages import SystemMessage as ChatSystemMessage
from langchain_core.runnables import Runnable
from langgraph.graph import END, START, MessagesState, StateGraph

from robinauts.adapters.config_file import ProviderKeys
from robinauts.domain import (
    NO_RESULT,
    AgentDefinition,
    AnswerCompleted,
    AnswerReasoningDelta,
    AnswerStarted,
    AnswerTextDelta,
    ConfigError,
    EngineEvent,
    InvalidValueError,
    Message,
    MessagePart,
    ModelConfig,
    ModelProviderConfig,
    ModelsConfig,
    ProviderKind,
    Role,
    ToolCallArgumentsDelta,
    ToolCallCompleted,
    ToolCallPart,
    ToolCallStarted,
    ToolDefinition,
    UnsupportedContentError,
    WaitingOnTools,
    chain,
    checked_data,
    clean_text,
    text_parts,
    tools_for_request,
    unanswered_calls,
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

ANSWER_NODE = "answer"
"""The one node of the graph: the model, streamed."""

VENDOR = "anthropic"
"""The key the vendor's opaque blocks are kept under in a message's ``extras``.

One vendor's key for one vendor's blocks (``docs/specs/conversations.md``,
"Reasoning"): the two kinds this engine reaches speak Anthropic's Messages
API, so what it stores and what it replays are Anthropic's thinking blocks,
and an adapter reaching another vendor reads past this key.
"""

THINKING_BLOCKS = frozenset({"thinking", "redacted_thinking"})
"""The vendor's block types that carry signed reasoning.

What the vendor requires back, unchanged, when a tool call's results go back:
a ``thinking`` block with its ``signature``, or a ``redacted_thinking`` block
that is opaque throughout. These are what ``_extras`` keeps and ``_assistant``
replays; the reasoning a person watched arrive is shown from the stream and
kept in the run's events, and never read out of these.
"""

BLOCKS_LEFT_OUT = (
    "the vendor's signed thinking blocks did not fit a message's extras and were left"
    " out; if the model asked for tools, the vendor may refuse the next round"
)
"""What the log says when an answer's blocks are bigger than ``extras`` may be.

The plan's open question, answered here for this iteration
(``docs/working-notes/mcp-plan.md``, "Open"): the answer is stored without
them rather than the turn failing over the size of the thinking, and the
line says what that may cost.
"""


ChatModelFactory = Callable[[ModelConfig, ModelProviderConfig, str], BaseChatModel]
"""How a chat model is built: the model, its provider, and the provider's key.

Injectable so that the tests run the engine over a chat model they script --
the graph, the streaming, the mapping and the releasing are then exercised for
real with no network and no key (``docs/layout.md``, "Testing strategy").
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


class LangGraphAgent(Agent):
    """The LangGraph engine: one turn, one graph, compiled and thrown away."""

    kinds: frozenset[ProviderKind] = frozenset(
        {ProviderKind.ANTHROPIC, ProviderKind.ANTHROPIC_COMPATIBLE}
    )
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
        *,
        chat_model_for: ChatModelFactory = chat_model,
    ) -> None:
        force_tracing_off()
        clear_client_overrides()
        quiet_client_logging()
        self._models = models
        """The models a turn may run on, and the provider each is reached through."""
        self._keys = keys
        """The providers' keys, as start-up read them. It prints nothing."""
        self._chat_model_for = chat_model_for
        self._open = 0

    @property
    def held(self) -> int:
        """How many turns of this engine still hold a stream open.

        Zero once every turn has ended or been closed, which is the promise a
        cancelled run depends on (``robinauts.ports.agents``). It counts the
        engine's own graph streams; the model's stream lives inside one and
        goes with it.

        **One engine, one count, however many turns it is running.** A
        deployment shares one ``LangGraphAgent`` between every conversation,
        so this says "is anything still open", not "is *that* turn still
        open": a caller watching one turn while another is in flight reads
        the other one's stream in this number. Nothing in the platform needs
        the finer answer -- the application releases a turn by closing its
        own stream and never asks -- and the contract suite reads it between
        turns, one at a time, which is when the two questions have the same
        answer.
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
        model, compiling the graph, opening the stream -- happens inside the
        generator, so that a provider that refuses a key is a failure of the
        turn, reported by raising where the caller is iterating, and not an
        exception thrown at whoever asked for the stream.
        """
        return self._turn(agent, history, tuple(tools), model)

    async def _turn(
        self,
        agent: AgentDefinition,
        history: Sequence[Message],
        tools: tuple[ToolDefinition, ...],
        model_id: str,
    ) -> AsyncGenerator[EngineEvent, None]:
        # The run's model, never the agent's default: the conversation may
        # have been moved to another (``robinauts.ports.agents``).
        model = self._models.model_by_id(model_id)
        provider = self._models.provider_for(model)
        chat = self._chat_model_for(model, provider, self._keys.key_for(provider.id))
        # The tools the run has, bound as the vendor's own definitions and
        # never executed here: a call is yielded and the turn ends. The port
        # is a seam a test double crosses too, so what comes over it is
        # checked to be the platform's definition and nothing that looks
        # like one.
        for tool in tools:
            if not isinstance(tool, ToolDefinition):
                raise InvalidValueError(f"a run's tools are ToolDefinitions, not {tool!r}")
        # With a stub for every name the history calls that the run lacks:
        # the vendor refuses tool blocks its request defines no tool for
        # (``core.tools_for_request``).
        defined = tools_for_request(tools, history)
        bound: Runnable[Any, Any] = (
            chat.bind_tools([_anthropic_tool(tool) for tool in defined]) if defined else chat
        )
        graph = _compiled(bound)
        answer = _Answer()
        whole: AIMessage | None = None
        self._open += 1
        try:
            stream = graph.astream(
                {"messages": _messages(agent, history, model_id)},
                stream_mode=["messages", "updates"],
                # A fresh configuration each turn, carrying no callbacks: a
                # turn inherits nothing from whatever context it happens to
                # run in. What keeps a tracer away is `force_tracing_off`;
                # this keeps everything else away.
                config={"callbacks": []},
            )
            async with aclosing(stream):
                async for mode, payload in stream:
                    if mode == "messages":
                        chunk, _metadata = payload
                        for event in answer.events_of(chunk):
                            yield event
                    else:
                        whole = _reply(payload)
            for event in answer.complete(whole):
                yield event
        finally:
            # Reached when the turn ends, when it raises, and when the
            # iteration is closed -- which is what a cancellation does.
            self._open -= 1


def _compiled(chat: Runnable[Any, Any]) -> Any:
    """The turn's graph: one node that streams the model, and no checkpointer.

    Compiled per turn and thrown away with it. Cheap -- it is a handful of
    objects, not a client or a connection -- and the alternative would be a
    graph held between turns, which is the state ADR 0002 says an engine does
    not keep.

    The node streams rather than invokes: a model asked for the whole answer
    at once would arrive as one piece however well it streams, and what a
    person watches arrive is what the platform stores. ``chat`` is the model
    with the run's tools bound, or the model alone; there is no tool node,
    because the platform runs the tools (``docs/specs/agents.md``).
    """

    async def answer(state: MessagesState) -> dict[str, list[BaseMessage]]:
        reply: BaseMessage | None = None
        async for chunk in chat.astream(state["messages"]):
            reply = chunk if reply is None else reply + chunk  # type: ignore[operator]
        return {"messages": [reply] if reply is not None else []}

    graph: StateGraph[Any, Any, Any, Any] = StateGraph(MessagesState)
    graph.add_node(ANSWER_NODE, answer)
    graph.add_edge(START, ANSWER_NODE)
    graph.add_edge(ANSWER_NODE, END)
    return graph.compile()


def _anthropic_tool(tool: ToolDefinition) -> dict[str, Any]:
    """The definition as the vendor's client takes it, as it stands.

    Anthropic's own shape -- ``name``, ``description``, ``input_schema`` --
    which ``bind_tools`` passes through untouched. The name is the full name
    the platform gave it, and the schema is the server's, as plain data. An
    empty description is left out rather than sent as ``""``: MCP's is
    optional, the vendor takes a tool without one, and the other engine sends
    the same bytes for the same definition (``docs/specs/agents.md``).
    """
    definition: dict[str, Any] = {"name": tool.name, "input_schema": dict(tool.input_schema)}
    if tool.description:
        definition["description"] = tool.description
    return definition


def _messages(
    agent: AgentDefinition, history: Sequence[Message], model_id: str
) -> list[BaseMessage]:
    """The history as the framework's messages, with the system prompt in front.

    The system prompt is the **agent's** and is not one of the messages
    (``docs/specs/conversations.md``), so it is put here, at every turn, from
    the definition as it stands now. An empty one is left out rather than sent
    as an empty system message, which some providers refuse.

    **Text, calls and results.** The reasoning a previous turn streamed is
    not carried back to the model as content: what is stored of it is the
    platform's record, not the vendor's, and the vendor's own signed blocks
    travel in ``extras`` and are replayed by ``_assistant`` on their own
    terms. A message with no text at all still
    becomes a message, empty, because dropping it *here* would be this engine
    deciding what a turn that said nothing means. What becomes of it is the
    vendor mapping's, and it is the same answer under both engines:
    Anthropic's API refuses an empty content block, so langchain-anthropic
    drops an assistant message whose content came out empty and Pydantic AI
    leaves out the assistant message it emptied. The model is shown the same
    history either way, which is what the swap needs.

    An answer's tool calls travel as the framework's ``tool_calls``, and a
    **tool message** becomes one ``ToolMessage`` per result, each naming its
    call and whether it went wrong -- which langchain-anthropic folds into the
    one ``user`` turn of ``tool_result`` blocks the vendor wants back. An
    answer whose calls no tool message answers is followed by one error
    result per call saying no result of it was recorded (``domain.NO_RESULT``).
    """
    messages: list[BaseMessage] = []
    if agent.system_prompt:
        messages.append(ChatSystemMessage(agent.system_prompt))
    unanswered = unanswered_calls(history)
    for message in history:
        if message.role is Role.USER:
            messages.append(HumanMessage(message.text))
        elif message.role is Role.ASSISTANT:
            messages.append(_assistant(message, model_id))
            # A call no tool message answers is answered here with what the
            # record says of it (``domain.NO_RESULT``): the vendor refuses a
            # call with nothing answering it, and the record, which keeps the
            # call without a result, is not what is edited.
            messages.extend(
                _tool_message(call.call_id, NO_RESULT, is_error=True)
                for call in unanswered.get(message.id, ())
            )
        else:
            messages.extend(
                _tool_message(part.call_id, part.text, is_error=part.is_error)
                for part in message.tool_results
            )
    return messages


def _tool_message(call_id: str, text: str, *, is_error: bool) -> ToolMessage:
    """One result as the framework's message, naming its call and how it went."""
    return ToolMessage(
        content=text, tool_call_id=call_id, status="error" if is_error else "success"
    )


def _assistant(message: Message, model_id: str) -> AIMessage:
    """An answer as the framework's message: its text, its calls, its signed blocks.

    **The vendor's signed thinking blocks are replayed to the model that made
    them** (``docs/specs/conversations.md``, "Reasoning"): they are bound to
    it, so an answer produced on another model is sent without them and
    nothing else is lost. They go in front of the text, where the vendor put
    them, exactly as they were stored; nothing here reads them. A plain answer
    -- no calls, no blocks -- is the one string it always was.
    """
    text = message.text
    calls = [
        {"name": part.name, "args": dict(part.arguments), "id": part.call_id, "type": "tool_call"}
        for part in message.tool_calls
    ]
    blocks = _replayed(message, model_id)
    if not calls and not blocks:
        return AIMessage(text)
    content: list[dict[str, Any]] = [*blocks]
    if text:
        content.append({"type": "text", "text": text})
    return AIMessage(content=content, tool_calls=calls)


def _replayed(message: Message, model_id: str) -> list[dict[str, Any]]:
    """The vendor's blocks stored on that answer, if they are for this model."""
    if message.provenance is None or message.provenance.model != model_id:
        return []
    kept = message.extras.get(VENDOR)
    if not isinstance(kept, Mapping):
        return []
    blocks = kept.get("thinking")
    if not isinstance(blocks, list):
        return []
    return [
        dict(block)
        for block in blocks
        if isinstance(block, Mapping) and block.get("type") in THINKING_BLOCKS
    ]


class _Answer:
    """One answer as it streams: what was said, what was called, and the events.

    The order the port asks for (``robinauts.core.check_engine_events``) is
    kept here: the answer is announced on its first piece, a call is announced
    when the client lifts it off the stream and completed when the next one
    begins or the answer ends, and what the call completes with is what its
    deltas parse to.
    """

    def __init__(self) -> None:
        self.started = False
        self.streamed: list[str] = []
        self.calls: list[ToolCallPart] = []
        self._open: tuple[str, str] | None = None
        """The call being made -- its id and name -- while its arguments stream."""
        self._arguments: list[str] = []

    def events_of(self, chunk: Any) -> list[EngineEvent]:
        """The events one streamed chunk carries, in the order they came."""
        if not isinstance(chunk, AIMessageChunk):
            # A model that does not stream: LangGraph passes the whole
            # ``AIMessage`` through here unchanged, and the answer is built
            # from the node's update in ``complete`` instead.
            return []
        events: list[EngineEvent] = []
        for block in chunk.content_blocks:
            kind = block.get("type")
            if kind == "text" and block.get("text"):
                text = str(block["text"])
                self.streamed.append(text)
                events.append(AnswerTextDelta(text=text))
            elif kind == "reasoning" and block.get("reasoning"):
                events.append(AnswerReasoningDelta(text=str(block["reasoning"])))
        for piece in chunk.tool_call_chunks:
            call_id, name, arguments = piece.get("id"), piece.get("name"), piece.get("args")
            if call_id and name:
                # A new call: the one before it, if any, is whole.
                events.extend(self._closed(None))
                self._open, self._arguments = (str(call_id), str(name)), []
                events.append(ToolCallStarted(call_id=str(call_id), name=str(name)))
            if arguments:
                if self._open is None:
                    raise UnsupportedContentError(
                        "the model streamed arguments for no tool call this engine announced"
                    )
                self._arguments.append(str(arguments))
                events.append(ToolCallArgumentsDelta(call_id=self._open[0], text=str(arguments)))
        if events and not self.started:
            self.started = True
            events.insert(0, AnswerStarted())
        return events

    def complete(self, whole: AIMessage | None) -> list[EngineEvent]:
        """The events that end the answer, given the message the node left in the state.

        ``whole`` is what an answer that never streamed completes with, where a
        call still open here that streamed no arguments takes its arguments
        from, and where the signed blocks are read off. A call the framework's final message holds
        that was never announced -- a block the client did not lift off the
        stream -- is refused rather than dropped: an answer missing a call
        would be half an answer that looks whole.
        """
        events: list[EngineEvent] = list(self._closed(whole))
        if not self.started:
            # Nothing was streamed that this version carries: the answer is
            # whatever the node left in the state, announced and completed in
            # one breath -- an engine is never required to stream.
            events.append(AnswerStarted())
            self.started = True
            for call in _calls_of(whole):
                events.append(ToolCallStarted(call_id=call.call_id, name=call.name))
                events.append(ToolCallCompleted(call=call))
                self.calls.append(call)
        announced = {call.call_id for call in self.calls}
        if any(call_id not in announced for call_id in _tool_use_ids(whole)):
            raise UnsupportedContentError(
                "the model asked for a tool in a form this engine did not translate"
            )
        parts: list[MessagePart] = []
        text = clean_text("".join(self.streamed)) if self.streamed else _text_of(whole)
        if text or not self.calls:
            parts.extend(text_parts(text))
        parts.extend(self.calls)
        events.append(AnswerCompleted(parts=tuple(parts), extras=_extras(whole)))
        if self.calls:
            events.append(WaitingOnTools())
        return events

    def _closed(self, whole: AIMessage | None) -> list[EngineEvent]:
        """Complete the call being made, if there is one, with what it streamed."""
        if self._open is None:
            return []
        call_id, name = self._open
        joined = clean_text("".join(self._arguments))
        if joined.strip():
            arguments = _parsed(joined)
        else:
            arguments = next(
                (call.arguments for call in _calls_of(whole) if call.call_id == call_id), {}
            )
        call = ToolCallPart(call_id=call_id, name=name, arguments=arguments)
        self.calls.append(call)
        self._open, self._arguments = None, []
        return [ToolCallCompleted(call=call)]


def _parsed(arguments: str) -> dict[str, Any]:
    """The JSON the model wrote for a call, as the object it has to be."""
    try:
        parsed = json.loads(arguments)
    except (ValueError, RecursionError):
        raise UnsupportedContentError(
            "the model's arguments for a tool call were not JSON"
        ) from None
    if not isinstance(parsed, dict):
        raise UnsupportedContentError("the model's arguments for a tool call were not an object")
    return parsed


def _reply(payload: Any) -> AIMessage | None:
    """The message the node put in the state, if it put one there."""
    if not isinstance(payload, Mapping):  # pragma: no cover -- LangGraph yields these
        return None
    update = payload.get(ANSWER_NODE)
    if not isinstance(update, Mapping):  # pragma: no cover -- our own node's shape
        return None
    messages = update.get("messages") or []
    return next((message for message in messages if isinstance(message, AIMessage)), None)


def _calls_of(whole: AIMessage | None) -> list[ToolCallPart]:
    """The calls the framework's final message holds, as the platform's parts."""
    if whole is None:
        return []
    calls: list[ToolCallPart] = []
    for call in whole.tool_calls:
        arguments = call.get("args")
        calls.append(
            ToolCallPart(
                call_id=str(call.get("id") or ""),
                name=str(call.get("name") or ""),
                arguments=arguments if isinstance(arguments, Mapping) else {},
            )
        )
    return calls


def _tool_use_ids(whole: AIMessage | None) -> list[str]:
    """The ids of the vendor's own ``tool_use`` blocks in the final message."""
    if whole is None or isinstance(whole.content, str):
        return []
    return [
        str(block.get("id"))
        for block in whole.content
        if isinstance(block, Mapping) and block.get("type") == "tool_use"
    ]


def _text_of(whole: AIMessage | None) -> str:
    """A framework message's text, and none of its reasoning.

    ``BaseMessage.text`` is the text blocks alone, which is exactly the line
    this version draws: thinking is shown as it arrives and is never part of
    what is stored (``docs/specs/conversations.md``).
    """
    if whole is None:
        return ""
    text = getattr(whole, "text", "")
    return clean_text(text) if isinstance(text, str) else ""


def _extras(whole: AIMessage | None) -> dict[str, Any]:
    """The vendor's signed blocks off the final message, keyed by vendor.

    Kept as they came, less the stream's own ``index``, and bounded as every
    ``extras`` is: blocks that do not fit are left out with a line in the log
    (``BLOCKS_LEFT_OUT``) rather than failing the turn over the size of the
    thinking.
    """
    if whole is None or isinstance(whole.content, str):
        return {}
    blocks = [
        {key: value for key, value in block.items() if key != "index"}
        for block in whole.content
        if isinstance(block, Mapping) and block.get("type") in THINKING_BLOCKS
    ]
    if not blocks:
        return {}
    extras = {VENDOR: {"thinking": blocks}}
    try:
        return checked_data(extras, "an answer's extras")
    except InvalidValueError as too_big:
        _log.warning("%s: %s", BLOCKS_LEFT_OUT, chain(too_big))
        return {}
