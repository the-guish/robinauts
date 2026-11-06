# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The Pydantic AI engine. A future extra: ``robinauts[pydantic-ai]``."""

from __future__ import annotations

from robinauts.agent_engines.contract.ports import (
    AgentEngine,
    EngineSettings,
    StorageConfig,
    StorageKind,
)
from robinauts.agent_engines.pydantic_ai_engine.engine import PydanticAIEngine
from robinauts.agent_engines.pydantic_ai_engine.session_store import (
    InProcessSessionStore,
    PostgresSessionStore,
)


def init_pydantic_ai(settings: EngineSettings, storage: StorageConfig) -> AgentEngine:
    """The engine over the storage asked: its sessions in the controller's PostgreSQL, else
    kept in this process."""
    if storage.kind is StorageKind.POSTGRES:
        return PydanticAIEngine(settings, PostgresSessionStore(storage.options["pool"]))
    return PydanticAIEngine(settings, InProcessSessionStore())
