# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The Pydantic AI engine. A future extra: ``robinauts[pydantic-ai]``."""

from __future__ import annotations

from pathlib import Path

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
    SQLiteSessionStore,
)


def init_pydantic_ai(settings: EngineSettings, storage: StorageConfig) -> AgentEngine:
    """The engine over the storage asked: its sessions on PostgreSQL, in a file in the
    worker's folder, or kept in this process."""
    if storage.kind is StorageKind.POSTGRES:
        return PydanticAIEngine(settings, PostgresSessionStore(storage.options["pool"]))
    if storage.kind is StorageKind.LOCAL:
        return PydanticAIEngine(settings, SQLiteSessionStore(Path(storage.options["path"])))
    return PydanticAIEngine(settings, InProcessSessionStore())
