# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The agent's tools, as toolsets of its MCP servers."""

from __future__ import annotations

from typing import Any

from pydantic_ai.toolsets import AbstractToolset

from robinauts.agent_engines.contract.domain import AgentDefinition
from robinauts.agent_engines.contract.ports import EngineSettings


async def toolsets_for(
    agent: AgentDefinition, settings: EngineSettings
) -> list[AbstractToolset[Any]]:
    return []
