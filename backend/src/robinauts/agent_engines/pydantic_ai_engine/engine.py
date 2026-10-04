# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The Pydantic AI engine: an agent per turn over the history it keeps per checkpoint.

The engine is handed its memory; ``init_pydantic_ai`` picks it for the storage asked, and
nothing here knows which storage that was.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Iterator
from contextlib import aclosing

from pydantic_ai import Agent, AgentRunResult, UsageLimits
from pydantic_ai.messages import (
    AgentStreamEvent,
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    ModelMessage,
    PartDeltaEvent,
    PartStartEvent,
    RetryPromptPart,
    TextPart,
    TextPartDelta,
    ThinkingPart,
    ThinkingPartDelta,
)

from robinauts.agent_engines.contract.domain import (
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
from robinauts.agent_engines.contract.ports import (
    DEFAULT_MAX_MODEL_CALLS,
    AgentEngine,
    EngineSettings,
)
from robinauts.agent_engines.pydantic_ai_engine.clients import chat_model, force_tracing_off
from robinauts.agent_engines.pydantic_ai_engine.memory import Memory
from robinauts.agent_engines.pydantic_ai_engine.tools import toolsets_for


class PydanticAIEngine(AgentEngine):
    def __init__(self, settings: EngineSettings, memory: Memory) -> None:
        self._settings = settings
        self._memory = memory
        force_tracing_off()

    def kinds(self) -> frozenset[ProviderKind]:
        return frozenset(ProviderKind)

    async def setup(self) -> None:
        await self._memory.setup()

    async def create(self, session_id: uuid.UUID) -> None:
        await self._memory.create(session_id)

    async def exists(self, session_id: uuid.UUID) -> bool:
        return await self._memory.exists(session_id)

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
        max_model_calls: int = DEFAULT_MAX_MODEL_CALLS,
    ) -> AsyncGenerator[Event, None]:
        if not await self._memory.exists(session_id):
            raise SessionNotFoundError(str(session_id))
        history = None
        if checkpoint_id is not None:
            history = await self._memory.history(session_id, checkpoint_id)
            if history is None:
                raise CheckpointNotFoundError(checkpoint_id)

        client, model_settings = chat_model(model, self._settings)
        runner = Agent(
            client,
            instructions=agent.system_prompt,
            model_settings=model_settings,
            toolsets=toolsets_for(agent, self._settings),
        )
        limits = UsageLimits(request_limit=max_model_calls)
        # The deadline bounds the run, not the caller's handling of what is yielded.
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        async with aclosing(run_of(runner, prompt, history, limits)) as items:
            while True:
                async with asyncio.timeout_at(deadline):
                    item = await anext(items, None)
                if item is None:
                    break
                if isinstance(item, AgentRunResult):
                    new = str(uuid.uuid4())
                    await self._memory.save(session_id, new, item.all_messages())
                    yield Done(text=item.output, checkpoint_id=new)
                else:
                    for event in events_of(item):
                        yield event

    async def fork(self, source_id: uuid.UUID, target_id: uuid.UUID, *, checkpoint_id: str) -> None:
        raise NotImplementedError("fork")

    async def forget(self, session_id: uuid.UUID) -> None:
        await self._memory.forget(session_id)


async def run_of(
    runner: Agent[None, str],
    prompt: str,
    history: list[ModelMessage] | None,
    limits: UsageLimits,
) -> AsyncIterator[AgentStreamEvent | AgentRunResult[str]]:
    async with runner.iter(prompt, message_history=history, usage_limits=limits) as run:
        async for node in run:
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
