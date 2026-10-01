# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The LangChain engine: a LangGraph agent per turn over a saver whose thread is the session."""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator

from langchain.agents import create_agent
from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver

from robinauts.agent_engines.contract.domain import (
    AgentDefinition,
    CheckpointNotFoundError,
    Done,
    Event,
    ProviderKind,
    SessionExistsError,
    SessionNotFoundError,
)
from robinauts.agent_engines.contract.ports import (
    AgentEngine,
    EngineSettings,
    StorageConfig,
)
from robinauts.agent_engines.langchain_engine.clients import chat_model, force_tracing_off


class LangChainEngine(AgentEngine):
    def __init__(self, settings: EngineSettings, storage: StorageConfig) -> None:
        self._settings = settings
        self._saver = InMemorySaver()
        self._sessions: set[uuid.UUID] = set()
        force_tracing_off()

    def kinds(self) -> frozenset[ProviderKind]:
        return frozenset(ProviderKind)

    async def setup(self) -> None:
        pass

    async def create(self, session_id: uuid.UUID) -> None:
        if session_id in self._sessions:
            raise SessionExistsError(str(session_id))
        self._sessions.add(session_id)

    async def exists(self, session_id: uuid.UUID) -> bool:
        return session_id in self._sessions

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
        if session_id not in self._sessions:
            raise SessionNotFoundError(str(session_id))
        thread: RunnableConfig = {"configurable": {"thread_id": str(session_id)}}
        start: RunnableConfig = thread
        if checkpoint_id is not None:
            start = {"configurable": {"thread_id": str(session_id), "checkpoint_id": checkpoint_id}}
            if await self._saver.aget_tuple(start) is None:
                raise CheckpointNotFoundError(checkpoint_id)

        graph = create_agent(
            chat_model(model, self._settings),
            system_prompt=agent.system_prompt,
            checkpointer=self._saver,
        )
        result = await graph.ainvoke({"messages": [HumanMessage(prompt)]}, start)
        state = await graph.aget_state(thread)
        yield Done(
            text=str(result["messages"][-1].text),
            checkpoint_id=state.config["configurable"]["checkpoint_id"],
        )

    async def fork(self, source_id: uuid.UUID, target_id: uuid.UUID, *, checkpoint_id: str) -> None:
        raise NotImplementedError("fork")

    async def forget(self, session_id: uuid.UUID) -> None:
        self._sessions.discard(session_id)
        await self._saver.adelete_thread(str(session_id))
