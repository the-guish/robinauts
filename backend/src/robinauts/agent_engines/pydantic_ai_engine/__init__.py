# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The Pydantic AI engine. A future extra: ``robinauts[pydantic-ai]``."""

from __future__ import annotations

from robinauts.agent_engines.contract.ports import AgentEngine, EngineSettings, StorageConfig
from robinauts.agent_engines.pydantic_ai_engine.engine import PydanticAIEngine


async def init_pydantic_ai(settings: EngineSettings, storage: StorageConfig) -> AgentEngine:
    return PydanticAIEngine(settings, storage)
