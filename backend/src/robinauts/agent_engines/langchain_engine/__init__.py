# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The LangChain engine, with LangGraph underneath. A future extra: ``robinauts[langchain]``."""

from __future__ import annotations

from robinauts.agent_engines.contract.ports import (
    AgentEngine,
    EngineSettings,
    StorageConfig,
    StorageKind,
)
from robinauts.agent_engines.langchain_engine.engine import LangChainEngine
from robinauts.agent_engines.langchain_engine.memory import InProcessMemory, PostgresMemory


async def init_langchain(settings: EngineSettings, storage: StorageConfig) -> AgentEngine:
    """The engine over the storage asked: its memory in the controller's PostgreSQL, else
    kept in this process."""
    if storage.kind is StorageKind.POSTGRES:
        return LangChainEngine(settings, PostgresMemory(storage.options["pool"]))
    return LangChainEngine(settings, InProcessMemory())
