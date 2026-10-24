# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The kataloop engine: our own loop, kataloop, over uni-llm-client. A future extra:
``robinauts[kataloop]``."""

from __future__ import annotations

from kataloop import MemoryStore

from robinauts.agent_engines.contract.ports import AgentEngine, EngineSettings, StorageConfig
from robinauts.agent_engines.kataloop_engine.engine import KataloopEngine


def init_kataloop(settings: EngineSettings, storage: StorageConfig) -> AgentEngine:
    """The engine over kataloop's memory in this process, whatever the storage asked:
    ``kataloop[postgres]``, its store over PostgreSQL, is not written yet. Nothing is opened."""
    return KataloopEngine(settings, MemoryStore())
