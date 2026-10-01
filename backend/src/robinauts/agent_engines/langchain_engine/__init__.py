# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The LangChain engine, with LangGraph underneath. A future extra: ``robinauts[langchain]``."""

from __future__ import annotations

from robinauts.agent_engines.contract.ports import AgentEngine, EngineSettings, StorageConfig


async def init_langchain(settings: EngineSettings, storage: StorageConfig) -> AgentEngine:
    raise NotImplementedError("the LangChain engine")
