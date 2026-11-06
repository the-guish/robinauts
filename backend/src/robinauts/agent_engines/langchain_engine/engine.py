# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The LangChain engine: a LangGraph agent per turn over a saver whose thread is the session.

The engine is handed its session store; ``init_langchain`` picks it for the storage asked, and
nothing here knows which storage that was.
"""

from __future__ import annotations

import asyncio
import math
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable, Iterator
from contextlib import aclosing
from typing import Any

from langchain.agents import create_agent
from langchain.agents.middleware import (
    AgentMiddleware,
    SummarizationMiddleware,
    ToolCallRequest,
    ToolErrorMiddleware,
)
from langchain_anthropic.middleware import AnthropicPromptCachingMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    RemoveMessage,
    ToolMessage,
)
from langchain_core.runnables import RunnableConfig
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, StateSnapshot
from mcp.shared.exceptions import McpError

from robinauts.agent_engines.contract.domain import (
    DEFAULT_CONTEXT_WINDOW,
    AgentDefinition,
    CheckpointNotFoundError,
    Done,
    Event,
    FunctionToolError,
    ProviderKind,
    ReasoningDelta,
    SessionNotFoundError,
    TextDelta,
    ToolCall,
    ToolResult,
)
from robinauts.agent_engines.contract.ports import AgentEngine, EngineSettings
from robinauts.agent_engines.langchain_engine.clients import (
    ANTHROPIC_KINDS,
    chat_model,
    force_tracing_off,
)
from robinauts.agent_engines.langchain_engine.session_store import SessionStore
from robinauts.agent_engines.langchain_engine.tools import function_tools, tools_for

FASTEST_MODEL_CALL = 1.0
"""Seconds: a turn may make one model call per second of its timeout. Real rounds are slower;
a loop faster than that is runaway, and this bounds what it costs."""

SUMMARIZE_AT = 0.7
"""The share of the model's window past which older messages are summarised."""

KEEP = 0.3
"""The share of the window kept as it is, the newest messages, when they are."""

LARGEST_RESULT = 0.2
"""The share of the window one tool result may take; past it, the rest is cut. A summary keeps
a tool call with its result, so one result larger than the window would never fit otherwise."""

CHARS_PER_TOKEN = 4


class LangChainEngine(AgentEngine):
    def __init__(self, settings: EngineSettings, session_store: SessionStore) -> None:
        self._settings = settings
        self._session_store = session_store
        force_tracing_off()

    def kinds(self) -> frozenset[ProviderKind]:
        return frozenset(ProviderKind)

    async def setup(self) -> None:
        await self._session_store.setup()

    async def create(self, session_id: uuid.UUID) -> None:
        await self._session_store.create(session_id)

    async def exists(self, session_id: uuid.UUID) -> bool:
        return await self._session_store.exists(session_id)

    async def stream(
        self,
        session_id: uuid.UUID,
        agent: AgentDefinition,
        prompt: str,
        *,
        model: str,
        checkpoint_id: str | None,
        timeout_seconds: float,
        resume: bool = False,
    ) -> AsyncGenerator[Event, None]:
        if not await self._session_store.exists(session_id):
            raise SessionNotFoundError(str(session_id))
        thread: RunnableConfig = {"configurable": {"thread_id": str(session_id)}}
        # Without a checkpoint the saver loads the thread's latest state: clear its messages.
        start: RunnableConfig = thread
        fresh: list[BaseMessage] = [RemoveMessage(id=REMOVE_ALL_MESSAGES)]
        if checkpoint_id is not None:
            fresh = []
            start = {"configurable": {"thread_id": str(session_id), "checkpoint_id": checkpoint_id}}
            if await self._session_store.saver.aget_tuple(start) is None:
                raise CheckpointNotFoundError(checkpoint_id)

        chat = chat_model(model, self._settings)
        graph = create_agent(
            chat,
            [*await tools_for(agent, self._settings), *function_tools(agent.functions)],
            system_prompt=agent.system_prompt,
            checkpointer=self._session_store.saver,
            middleware=middleware_for(chat, model, self._settings),
        )
        given: dict[str, Any] | None = {"messages": [*fresh, HumanMessage(prompt)]}
        if resume:
            partial = await partial_turn(graph, thread, start, checkpoint_id)
            if partial is not None and not partial.next:
                # It had finished: its caller never stored the answer.
                yield done_of(partial)
                return
            if partial is not None:
                # LangGraph runs again what the latest checkpoint has left to run.
                given, start = None, thread
        # A model call and its tool round are two steps of the graph, and the summary's check
        # before the call a third.
        calls = math.ceil(timeout_seconds / FASTEST_MODEL_CALL)
        limited: RunnableConfig = {**start, "recursion_limit": 3 * calls + 1}
        stream = graph.astream(given, limited, stream_mode=["messages", "updates"])
        # The deadline bounds the run, not the caller's handling of what is yielded.
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        async with aclosing(stream):
            while True:
                async with asyncio.timeout_at(deadline):
                    item = await anext(stream, None)
                if item is None:
                    break
                for event in events_of(*item):
                    yield event
        yield done_of(await graph.aget_state(thread))

    async def fork(self, source_id: uuid.UUID, target_id: uuid.UUID, *, checkpoint_id: str) -> None:
        raise NotImplementedError("fork")

    async def forget(self, session_id: uuid.UUID) -> None:
        await self._session_store.forget(session_id)


def middleware_for(
    chat: BaseChatModel, model: str, settings: EngineSettings
) -> list[AgentMiddleware[Any, Any, Any]]:
    """A failed tool call told to the model; older messages summarised as the window fills; and
    on Anthropic's protocol, the prompt cached, which OpenAI's does by itself."""
    config = settings.models.models[model]
    profile = chat.profile or {}
    window = config.context_window or profile.get("max_input_tokens") or DEFAULT_CONTEXT_WINDOW
    middleware: list[AgentMiddleware[Any, Any, Any]] = [
        CutLargeResults(int(window * LARGEST_RESULT) * CHARS_PER_TOKEN),
        ToolErrorMiddleware(tool_failed),
        SummarizationMiddleware(
            chat,
            trigger=("tokens", int(window * SUMMARIZE_AT)),
            keep=("tokens", int(window * KEEP)),
            # What is summarised must fit the window itself.
            trim_tokens_to_summarize=int(window * (SUMMARIZE_AT - KEEP)),
        ),
    ]
    if settings.models.providers[config.provider].kind in ANTHROPIC_KINDS:
        middleware.append(AnthropicPromptCachingMiddleware(unsupported_model_behavior="ignore"))
    return middleware


class CutLargeResults(AgentMiddleware[Any, Any, Any]):
    """A tool result longer than ``largest`` characters, cut there."""

    def __init__(self, largest: int) -> None:
        super().__init__()
        self.largest = largest

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        result = await handler(request)
        if not isinstance(result, ToolMessage) or len(result.text) <= self.largest:
            return result
        note = f"\n[Cut: the result was {len(result.text)} characters.]"
        return result.model_copy(update={"content": result.text[: self.largest] + note})


async def partial_turn(
    graph: CompiledStateGraph[Any, Any, Any, Any],
    thread: RunnableConfig,
    start: RunnableConfig,
    checkpoint_id: str | None,
) -> StateSnapshot | None:
    """The thread's latest state if it is a turn begun from the checkpoint, finished or not.
    Which question it asked is the caller's to know."""
    latest = await graph.aget_state(thread)
    before = [] if checkpoint_id is None else (await graph.aget_state(start)).values["messages"]
    after = latest.values.get("messages", [])
    if len(after) <= len(before) or [m.id for m in after[: len(before)]] != [m.id for m in before]:
        return None
    if not isinstance(after[len(before)], HumanMessage):
        return None
    return latest


def done_of(state: StateSnapshot) -> Done:
    return Done(
        text=str(state.values["messages"][-1].text),
        checkpoint_id=state.config["configurable"]["checkpoint_id"],
    )


def tool_failed(error: Exception, _request: ToolCallRequest) -> str:
    """What the model is told of a tool call that raised, so that the turn goes on: the MCP
    session's own words, such as a timeout's, a function tool's own message, or else the kind of
    error alone. An ``isError`` result of the server's is already its own answer."""
    if isinstance(error, McpError | FunctionToolError):
        return str(error)
    return f"The tool call failed: {type(error).__name__}."


AGENT_STEPS = ("model", "tools")
"""The graph's steps whose messages are the turn's. A middleware's are not: the summary's model
call, and the messages a summary keeps, which it writes again."""


def events_of(mode: str, payload: Any) -> Iterator[Event]:
    if mode == "messages":
        chunk, metadata = payload
        if metadata.get("langgraph_node") not in AGENT_STEPS:
            return
        if isinstance(chunk, AIMessageChunk):
            for block in chunk.content_blocks:
                if block["type"] == "text" and block["text"]:
                    yield TextDelta(block["text"])
                elif block["type"] == "reasoning" and block.get("reasoning"):
                    yield ReasoningDelta(block["reasoning"])
        return
    for step, update in payload.items():
        if step not in AGENT_STEPS or not update:
            continue
        for message in update["messages"]:
            if isinstance(message, AIMessage):
                for call in message.tool_calls:
                    yield ToolCall(str(call["id"]), call["name"], call["args"])
            elif isinstance(message, ToolMessage):
                yield ToolResult(
                    message.tool_call_id, str(message.name), message.text, message.status == "error"
                )
