# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The echo engine: no model, no network, one tool. Shipped for integration tests."""

from __future__ import annotations

from robinauts.agent_engines.contract.ports import AgentEngine, EngineSettings, StorageConfig
from robinauts.agent_engines.echo_engine.engine import EchoEngine


async def init_echo(settings: EngineSettings, storage: StorageConfig) -> AgentEngine:
    return EchoEngine()
