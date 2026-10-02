# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The LangChain engine, with LangGraph underneath. A future extra: ``robinauts[langchain]``."""

from __future__ import annotations

from langgraph.checkpoint.memory import InMemorySaver

from robinauts.agent_engines.contract.ports import (
    AgentEngine,
    EngineSettings,
    StorageConfig,
    StorageKind,
)
from robinauts.agent_engines.langchain_engine.engine import LangChainEngine
from robinauts.agent_engines.langchain_engine.saver import (
    PostgresSaver,
    PostgresSessions,
    Sessions,
)


async def init_langchain(settings: EngineSettings, storage: StorageConfig) -> AgentEngine:
    """The engine over the storage asked: its own saver and sessions in the controller's
    PostgreSQL, else LangGraph's in-memory saver and sessions kept in this process."""
    if storage.kind is StorageKind.POSTGRES:
        pool = storage.options["pool"]
        return LangChainEngine(settings, PostgresSaver(pool), PostgresSessions(pool))
    return LangChainEngine(settings, InMemorySaver(), Sessions())
