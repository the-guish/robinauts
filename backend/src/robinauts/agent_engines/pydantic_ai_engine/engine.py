# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The Pydantic AI engine: an agent per turn over the history it keeps per checkpoint.

The engine is handed its session store; ``init_pydantic_ai`` picks it for the storage asked, and
nothing here knows which storage that was.
"""

from __future__ import annotations

import asyncio
import math
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Iterator
from contextlib import aclosing

from pydantic_ai import Agent, AgentRunResult, UsageLimits
from pydantic_ai.capabilities import ProcessHistory
from pydantic_ai.messages import (
    AgentStreamEvent,
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    PartDeltaEvent,
    PartStartEvent,
    RetryPromptPart,
    TextPart,
    TextPartDelta,
    ThinkingPart,
    ThinkingPartDelta,
    UserPromptPart,
)

from robinauts.agent_engines.contract.domain import (
    DEFAULT_CONTEXT_WINDOW,
    AgentDefinition,
    CheckpointNotFoundError,
    Done,
    Event,
    ProviderKind,
    ReasoningDelta,
    SessionNotFoundError,
    TextDelta,
    ToolCall,
    ToolResult,
)
from robinauts.agent_engines.contract.ports import AgentEngine, EngineSettings
from robinauts.agent_engines.pydantic_ai_engine.clients import (
    chat_model,
    force_tracing_off,
    release,
)
from robinauts.agent_engines.pydantic_ai_engine.context import within
from robinauts.agent_engines.pydantic_ai_engine.session_store import SessionStore
from robinauts.agent_engines.pydantic_ai_engine.tools import function_toolsets, toolsets_for

FASTEST_MODEL_CALL = 1.0
"""Seconds: a turn may make one model call per second of its timeout. Real rounds are slower;
a loop faster than that is runaway, and this bounds what it costs."""


class PydanticAIEngine(AgentEngine):
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
        history = None
        if checkpoint_id is not None:
            history = await self._session_store.history(session_id, checkpoint_id)
            if history is None:
                raise CheckpointNotFoundError(checkpoint_id)

        # One checkpoint per turn: saved after every tool round, and last with the answer.
        checkpoint = str(uuid.uuid4())
        asked: str | None = prompt
        if resume and (partial := await self._partial_turn(session_id, history)):
            checkpoint, history = partial
            if isinstance(history[-1], ModelResponse):
                # It had finished: its caller never stored the answer.
                yield Done(text=history[-1].text or "", checkpoint_id=checkpoint)
                return
            # It goes on from the tool results it saved, without asking again.
            asked = None

        async def save(messages: list[ModelMessage]) -> None:
            await self._session_store.save(session_id, checkpoint, messages)

        client, model_settings = chat_model(model, self._settings)
        try:
            configured = self._settings.models.models[model].context_window
            window = configured or client.context_window or DEFAULT_CONTEXT_WINDOW
            runner = Agent(
                client,
                instructions=agent.system_prompt,
                model_settings=model_settings,
                toolsets=[
                    *toolsets_for(agent, self._settings),
                    *function_toolsets(agent.functions),
                ],
                capabilities=[ProcessHistory(within(window))],
            )
            # The deadline bounds the run, not the caller's handling of what is yielded.
            deadline = asyncio.get_running_loop().time() + timeout_seconds
            calls = math.ceil(timeout_seconds / FASTEST_MODEL_CALL)
            async with aclosing(run_of(runner, asked, history, calls, save)) as items:
                while True:
                    async with asyncio.timeout_at(deadline):
                        item = await anext(items, None)
                    if item is None:
                        break
                    if isinstance(item, AgentRunResult):
                        await save(item.all_messages())
                        yield Done(text=item.output, checkpoint_id=checkpoint)
                    else:
                        for event in events_of(item):
                            yield event
        finally:
            # The vendor's client is the turn's: its connections go with it.
            await release(client)

    async def _partial_turn(
        self, session_id: uuid.UUID, history: list[ModelMessage] | None
    ) -> tuple[str, list[ModelMessage]] | None:
        """The checkpoint saved last and its history, if it is a turn begun from ``history``,
        finished or not. Which question it asked is the caller's to know."""
        latest = await self._session_store.latest(session_id)
        if latest is None:
            return None
        before = history or []
        after = latest[1]
        if len(after) <= len(before) or _rendered(after[: len(before)]) != _rendered(before):
            return None
        asked = after[len(before)]
        if not isinstance(asked, ModelRequest):
            return None
        if not any(isinstance(part, UserPromptPart) for part in asked.parts):
            return None
        return latest

    async def fork(self, source_id: uuid.UUID, target_id: uuid.UUID, *, checkpoint_id: str) -> None:
        raise NotImplementedError("fork")

    async def forget(self, session_id: uuid.UUID) -> None:
        await self._session_store.forget(session_id)


def _rendered(messages: list[ModelMessage]) -> object:
    return ModelMessagesTypeAdapter.dump_python(messages, mode="json")


async def run_of(
    runner: Agent[None, str],
    prompt: str | None,
    history: list[ModelMessage] | None,
    calls: int,
    save: Callable[[list[ModelMessage]], Awaitable[None]],
) -> AsyncIterator[AgentStreamEvent | AgentRunResult[str]]:
    limits = UsageLimits(request_limit=calls)
    async with runner.iter(prompt, message_history=history, usage_limits=limits) as run:
        async for node in run:
            # A request after the first carries a finished round's tool results, which the
            # history takes only once the request is sent: save them now.
            if Agent.is_model_request_node(node) and run.new_messages():
                await save([*run.all_messages(), node.request])
            if Agent.is_model_request_node(node) or Agent.is_call_tools_node(node):
                async with node.stream(run.ctx) as events:
                    async for event in events:
                        yield event
    yield run.result


def events_of(event: AgentStreamEvent) -> Iterator[Event]:
    if isinstance(event, PartStartEvent):
        if isinstance(event.part, TextPart) and event.part.content:
            yield TextDelta(event.part.content)
        elif isinstance(event.part, ThinkingPart) and event.part.content:
            yield ReasoningDelta(event.part.content)
    elif isinstance(event, PartDeltaEvent):
        if isinstance(event.delta, TextPartDelta) and event.delta.content_delta:
            yield TextDelta(event.delta.content_delta)
        elif isinstance(event.delta, ThinkingPartDelta) and event.delta.content_delta:
            yield ReasoningDelta(event.delta.content_delta)
    elif isinstance(event, FunctionToolCallEvent):
        yield ToolCall(event.part.tool_call_id, event.part.tool_name, event.part.args_as_dict())
    elif isinstance(event, FunctionToolResultEvent):
        part = event.part
        if isinstance(part, RetryPromptPart):
            yield ToolResult(part.tool_call_id, part.tool_name or "", part.model_response(), True)
        else:
            output = part.model_response_str(wrap_if_error=False)
            yield ToolResult(part.tool_call_id, part.tool_name, output, part.outcome != "success")
