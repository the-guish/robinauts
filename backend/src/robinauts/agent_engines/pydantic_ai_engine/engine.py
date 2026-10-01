# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The Pydantic AI engine: an agent per turn over the history it keeps per checkpoint."""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator

from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage

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
from robinauts.agent_engines.pydantic_ai_engine.clients import chat_model, force_tracing_off


class PydanticAIEngine(AgentEngine):
    def __init__(self, settings: EngineSettings, storage: StorageConfig) -> None:
        self._settings = settings
        self._sessions: dict[uuid.UUID, dict[str, list[ModelMessage]]] = {}
        force_tracing_off()

    def kinds(self) -> frozenset[ProviderKind]:
        return frozenset(ProviderKind)

    async def setup(self) -> None:
        pass

    async def create(self, session_id: uuid.UUID) -> None:
        if session_id in self._sessions:
            raise SessionExistsError(str(session_id))
        self._sessions[session_id] = {}

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
        checkpoints = self._sessions.get(session_id)
        if checkpoints is None:
            raise SessionNotFoundError(str(session_id))
        history = None
        if checkpoint_id is not None:
            history = checkpoints.get(checkpoint_id)
            if history is None:
                raise CheckpointNotFoundError(checkpoint_id)

        client, model_settings = chat_model(model, self._settings)
        runner = Agent(client, instructions=agent.system_prompt, model_settings=model_settings)
        result = await runner.run(prompt, message_history=history)
        new = str(uuid.uuid4())
        checkpoints[new] = result.all_messages()
        yield Done(text=result.output, checkpoint_id=new)

    async def fork(self, source_id: uuid.UUID, target_id: uuid.UUID, *, checkpoint_id: str) -> None:
        raise NotImplementedError("fork")

    async def forget(self, session_id: uuid.UUID) -> None:
        self._sessions.pop(session_id, None)
