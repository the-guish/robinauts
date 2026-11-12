# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The controller builds the engines its agents name, and refuses what they cannot run."""

from __future__ import annotations

from collections.abc import Mapping
from unittest.mock import create_autospec

import pytest

from robinauts.agent_engines.contract.domain import ProviderKind
from robinauts.agent_engines.contract.ports import AgentEngine, EngineFactory, installed
from robinauts.shared_domain import domain as shared
from robinauts.worker.engine_settings import engine_settings, engine_storage
from robinauts.worker.engines import build_engines
from util.aio import asyncio_test


def config(engine: str, kind: shared.ProviderKind = shared.ProviderKind.ANTHROPIC) -> shared.Config:
    return shared.Config(
        providers={"acme": shared.ProviderConfig("acme", kind, "ACME_KEY")},
        models={"m": shared.ModelConfig("m", "acme", "model-1")},
        agents={
            "a": shared.AgentConfig("a", "A", "", ("m",), engine),
            "b": shared.AgentConfig("b", "B", "", ("m",), engine),
        },
    )


async def build(
    config: shared.Config, factories: Mapping[str, EngineFactory]
) -> dict[str, AgentEngine]:
    settings = engine_settings(config, {"ACME_KEY": "sk-1"}.get)
    storage = engine_storage(shared.StorageConfig(shared.StorageKind.IN_PROCESS), None)
    return await build_engines(config, settings, storage, factories)


def anthropic_only(*_: object) -> AgentEngine:
    engine = create_autospec(AgentEngine, spec_set=True, instance=True)
    engine.kinds.return_value = frozenset({ProviderKind.ANTHROPIC})
    return engine


@pytest.mark.parametrize(
    ("engine", "kind", "error", "named"),
    [
        ("other", shared.ProviderKind.ANTHROPIC, shared.UnknownEngineError, "'other'"),
        ("fake", shared.ProviderKind.OPENAI, shared.UnreachableProviderError, "'openai'"),
    ],
)
@asyncio_test
async def test_an_engine_that_cannot_run_the_agent_is_refused_by_name(
    engine: str, kind: shared.ProviderKind, error: type[Exception], named: str
) -> None:
    with pytest.raises(error, match=named):
        await build(config(engine, kind), {"fake": anthropic_only})


@pytest.mark.parametrize(
    ("engine", "built_as"),
    [
        ("langchain", "LangChainEngine"),
        ("pydantic-ai", "PydanticAIEngine"),
        ("echo", "EchoEngine"),
    ],
)
@asyncio_test
async def test_an_installed_engine_is_built_by_name(engine: str, built_as: str) -> None:
    assert set(installed()) == {"langchain", "pydantic-ai", "echo"}
    engines = await build(config(engine), installed())
    assert type(engines[engine]).__name__ == built_as
    assert ProviderKind.OPENAI in engines[engine].kinds()
