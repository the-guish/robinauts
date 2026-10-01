# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The LangChain engine. An operation not yet implemented raises ``NotImplementedError``."""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator

from robinauts.agent_engines.contract.domain import AgentDefinition, Event, ProviderKind
from robinauts.agent_engines.contract.ports import (
    AgentEngine,
    EngineSettings,
    StorageConfig,
)


class LangChainEngine(AgentEngine):
    def __init__(self, settings: EngineSettings, storage: StorageConfig) -> None:
        self._settings = settings
        self._storage = storage

    def kinds(self) -> frozenset[ProviderKind]:
        return frozenset(ProviderKind)

    async def setup(self) -> None:
        pass  # nothing to make ready until the engine keeps sessions

    async def create(self, session_id: uuid.UUID) -> None:
        raise NotImplementedError("create")

    async def exists(self, session_id: uuid.UUID) -> bool:
        raise NotImplementedError("exists")

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
        raise NotImplementedError("stream")
        yield  # makes this the generator the port declares

    async def fork(self, source_id: uuid.UUID, target_id: uuid.UUID, *, checkpoint_id: str) -> None:
        raise NotImplementedError("fork")

    async def forget(self, session_id: uuid.UUID) -> None:
        raise NotImplementedError("forget")
