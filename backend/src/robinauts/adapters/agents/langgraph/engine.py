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
two edges. It would be shorter to call the model directly; then the seam the
project exists to prove would not be exercised, and the day a turn grows a
second node -- tools -- the shape would have to be invented rather than
extended.

**The mapping**, which is the whole of the translation:

- LangGraph's ``messages`` stream carries what the model produced, chunk by
  chunk. The first chunk with anything in it opens the answer
  (``AnswerStarted``); text becomes ``AnswerTextDelta`` and reasoning becomes
  ``AnswerReasoningDelta``;
- the node's own update carries the whole message, which is what an answer
  **that never streamed** completes with;
- **what was streamed is what is kept** (``docs/specs/agents.md``): an answer
  that yielded text deltas completes with exactly those deltas joined, never
  with whatever the framework made of the final message;
- **reasoning is never in the completed parts.** ``domain.kept_parts`` would
  drop a ``ReasoningPart`` anyway; putting the model's thinking in the answer's
  *text* is the mistake that would survive that, so the text part is built
  from the text deltas alone.

Chunks are read through ``AIMessageChunk.content_blocks``, langchain-core's
own provider-neutral view of a message's content: Anthropic's ``thinking``
blocks arrive here as standard ``reasoning`` blocks, so the engine needs no
vendor branch to show thinking as it arrives.

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

import logging
import os
from collections.abc import AsyncGenerator, Callable, Mapping, Sequence
from contextlib import aclosing
from typing import Any

import langsmith
from langchain_anthropic import ChatAnthropic
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, HumanMessage
from langchain_core.messages import SystemMessage as ChatSystemMessage
from langgraph.graph import END, START, MessagesState, StateGraph

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
        chat = self._chat_model_for(model, provider, self._keys.key_for(provider.id))
        graph = _compiled(chat)
        started = False
        streamed: list[str] = []
        whole: str | None = None
        self._open += 1
        try:
            stream = graph.astream(
                {"messages": _messages(agent, history)},
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
                        for text, reasoning in _blocks(chunk):
                            if not started:
                                started = True
                                yield AnswerStarted()
                            if reasoning:
                                yield AnswerReasoningDelta(text=reasoning)
                            if text:
                                streamed.append(text)
                                yield AnswerTextDelta(text=text)
                    else:
                        whole = _said(payload)
            if not started:
                # Nothing was streamed that this version carries -- no text
                # and no thinking. The answer is whatever the node left in the
                # state, announced and completed in one breath; an engine is
                # never required to stream (``docs/specs/agents.md``).
                yield AnswerStarted()
            yield AnswerCompleted(parts=_parts(streamed, whole))
        finally:
            # Reached when the turn ends, when it raises, and when the
            # iteration is closed -- which is what a cancellation does.
            self._open -= 1


def _compiled(chat: BaseChatModel) -> Any:
    """The turn's graph: one node that streams the model, and no checkpointer.

    Compiled per turn and thrown away with it. Cheap -- it is a handful of
    objects, not a client or a connection -- and the alternative would be a
    graph held between turns, which is the state ADR 0002 says an engine does
    not keep.

    The node streams rather than invokes: a model asked for the whole answer
    at once would arrive as one piece however well it streams, and what a
    person watches arrive is what the platform stores.
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


def _messages(agent: AgentDefinition, history: Sequence[Message]) -> list[BaseMessage]:
    """The history as the framework's messages, with the system prompt in front.

    The system prompt is the **agent's** and is not one of the messages
    (``docs/specs/conversations.md``), so it is put here, at every turn, from
    the definition as it stands now. An empty one is left out rather than sent
    as an empty system message, which some providers refuse.

    **Text only.** Reasoning a previous turn produced is not carried back to
    the model: this version keeps none of it, and what is stored holds none
    either (``docs/working-notes/poc-scope.md``). A message with no text at
    all still becomes a message, empty, because dropping it *here* would be
    this engine deciding what a turn that said nothing means. What becomes of
    it is the vendor mapping's, and it is the same answer under both engines:
    Anthropic's API refuses an empty content block, so langchain-anthropic
    drops an assistant message whose content came out empty and Pydantic AI
    leaves out the assistant message it emptied. The model is shown the same
    history either way, which is what the swap needs.

    **A message of the ``tool`` role is refused.** The role is reserved and
    not carried (``docs/specs/conversations.md``); sending one to a model as
    though the agent had said it would be telling the model something nobody
    said, and quietly. Nothing can put one in a conversation today, which is
    why this is a refusal rather than a translation: the day tool results are
    stored, this is the line that has to learn what they look like.
    """
    messages: list[BaseMessage] = []
    if agent.system_prompt:
        messages.append(ChatSystemMessage(agent.system_prompt))
    for message in history:
        if message.role not in (Role.USER, Role.ASSISTANT):
            raise UnsupportedContentError(
                f"a message of the {message.role.value} role cannot be sent to a model:"
                f" this version carries no tool results"
            )
        text = "".join(part.text for part in message.parts if isinstance(part, TextPart))
        messages.append(HumanMessage(text) if message.role is Role.USER else AIMessage(text))
    return messages


TOOL_BLOCKS = frozenset({"tool_call", "tool_call_chunk", "invalid_tool_call", "tool_use"})
"""The block kinds that mean the model wants to use a tool.

Both spellings: the standard one langchain-core normalises to, and the
vendor's own, which is what a block carries when it arrives as
``non_standard`` -- the framework passes a block it does not recognise
through rather than dropping it, and a tool call is exactly the kind of thing
it will not recognise until this version has tools.
"""

NO_TOOLS = (
    "the model asked to use a tool, and this version has none: give the agent a"
    " model or a system prompt that does not, or wait for tool usage"
)
"""Why a turn that meets a tool call fails, and what an operator can do."""

NO_TOOL_BINDING = (
    "the LangGraph engine does not bind tools to the model yet; a turn with tools is"
    " refused rather than run without them"
)
"""Why a turn handed tool definitions fails, until this engine binds them."""


def _blocks(chunk: Any) -> list[tuple[str, str]]:
    """The text and the reasoning of one streamed chunk, in the order they came.

    ``content_blocks`` is langchain-core's provider-neutral view of what a
    message holds, so Anthropic's ``thinking`` blocks arrive as standard
    ``reasoning`` ones and nothing here is vendor-specific. Blocks of a kind
    this version does not carry -- a citation, whatever a provider adds next
    -- are passed over: an engine that guessed at them would be inventing
    content.

    **A tool call is not passed over.** This version has no tools
    (``docs/specs/agents.md``), so a model that asks to use one is asking for
    something that will never happen: every following block would be part of
    an answer that cannot be produced, and passing the call over would finish
    the turn with whatever text happened to come with it -- an answer that
    looks complete and is half of one. An engine reports that by raising, and
    ``UnsupportedContentError`` is what the format already calls content this
    build does not carry.
    """
    if not isinstance(chunk, AIMessageChunk):  # pragma: no cover -- LangGraph yields these
        return []
    if chunk.tool_call_chunks or chunk.tool_calls or chunk.invalid_tool_calls:
        # How a provider that really streams a tool call arrives: the client
        # lifts it off the content and on to the message.
        raise UnsupportedContentError(NO_TOOLS)
    found: list[tuple[str, str]] = []
    for block in chunk.content_blocks:
        kind = block.get("type")
        if kind == "text":
            found.append((str(block.get("text", "")), ""))
        elif kind == "reasoning":
            found.append(("", str(block.get("reasoning", ""))))
        elif _asks_for_a_tool(kind, block):
            raise UnsupportedContentError(NO_TOOLS)
    return [pair for pair in found if pair != ("", "")]


def _asks_for_a_tool(kind: object, block: Mapping[str, Any]) -> bool:
    """Whether this block is the model asking to use a tool.

    Two ways round: the standard block kind, and a block the framework did not
    recognise and passed through under ``non_standard`` with the vendor's own
    spelling inside it -- which is where a tool call lands while this version
    has no tools to declare.
    """
    if kind in TOOL_BLOCKS:
        return True
    if kind != "non_standard":
        return False
    inner = block.get("value")
    return isinstance(inner, Mapping) and inner.get("type") in TOOL_BLOCKS


def _said(payload: Any) -> str | None:
    """The text of the message the node put in the state, if it put one there."""
    if not isinstance(payload, Mapping):  # pragma: no cover -- LangGraph yields these
        return None
    update = payload.get(ANSWER_NODE)
    if not isinstance(update, Mapping):  # pragma: no cover -- our own node's shape
        return None
    messages = update.get("messages") or []
    return "".join(_text_of(message) for message in messages)


def _text_of(message: Any) -> str:
    """A framework message's text, and none of its reasoning.

    ``BaseMessage.text`` is the text blocks alone, which is exactly the line
    this version draws: thinking is shown as it arrives and is never part of
    what is stored (``docs/specs/conversations.md``).
    """
    text = getattr(message, "text", "")
    return text if isinstance(text, str) else ""


def _parts(streamed: list[str], whole: str | None) -> tuple[TextPart, ...]:
    """What the answer completes with: what was streamed, or the whole message.

    **What was streamed wins.** A framework that rewrites the final message --
    reordering blocks, adding a summary, normalising whitespace -- would
    otherwise store something other than what a person watched arrive, which
    is the promise ``core.check_engine_events`` holds every engine to.

    Made storable on the way (``domain.clean_text``): a provider splits where
    it likes, so a character above the basic plane can arrive in halves, and
    the halves are joined here rather than published as something a store
    cannot hold. An answer with no text at all is one empty part -- which is
    ``text_parts``'s own answer for empty text -- because a message has
    content and "the model said nothing" is worth recording.
    """
    text = clean_text("".join(streamed)) if streamed else clean_text(whole or "")
    return text_parts(text)
