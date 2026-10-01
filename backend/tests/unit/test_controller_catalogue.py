# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""``list_agents`` and ``list_models`` answer the configuration, in its order."""

from __future__ import annotations

from aio import asyncio_test
from robinauts.controller.composition import build
from robinauts.controller.contract.domain import (
    AgentConfig,
    AgentListing,
    Config,
    ModelConfig,
    ModelListing,
    StorageConfig,
    StorageKind,
)


@asyncio_test
async def test_lists_the_configured_agents_and_models() -> None:
    config = Config(
        models={
            "fast": ModelConfig("fast", provider="p", name="fast-1", title="Fast"),
            "slow": ModelConfig("slow", provider="p", name="slow-1"),
        },
        agents={
            "a": AgentConfig("a", title="A", system_prompt="", model="fast", engine="echo"),
            "b": AgentConfig("b", title="B", system_prompt="", model="slow", engine="echo"),
        },
    )
    controller = build(config, storage=StorageConfig(StorageKind.IN_MEMORY), secret_for={}.get)
    assert await controller.list_agents() == (
        AgentListing("a", "A", default_model="fast"),
        AgentListing("b", "B", default_model="slow"),
    )
    assert await controller.list_models() == (
        ModelListing("fast", "Fast"),
        ModelListing("slow", "slow"),
    )
