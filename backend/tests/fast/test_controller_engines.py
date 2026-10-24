# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The controller builds the engines its agents name, and refuses what they cannot run."""

from __future__ import annotations

from collections.abc import Mapping
from unittest.mock import create_autospec

import pytest

from robinauts.agent_engines.contract.domain import ProviderKind
from robinauts.agent_engines.contract.ports import AgentEngine, EngineFactory, installed
from robinauts.controller.application.engines import build_engines
from robinauts.controller.contract import domain
from robinauts.controller.core.engine_settings import engine_settings, engine_storage
from util.aio import asyncio_test


def config(engine: str, kind: domain.ProviderKind = domain.ProviderKind.ANTHROPIC) -> domain.Config:
    return domain.Config(
        providers={"acme": domain.ProviderConfig("acme", kind, "ACME_KEY")},
        models={"m": domain.ModelConfig("m", "acme", "model-1")},
        agents={
            "a": domain.AgentConfig("a", "A", "", "m", engine),
            "b": domain.AgentConfig("b", "B", "", "m", engine),
        },
    )


async def build(
    config: domain.Config, factories: Mapping[str, EngineFactory]
) -> dict[str, AgentEngine]:
    settings = engine_settings(config, {"ACME_KEY": "sk-1"}.get)
    storage = engine_storage(domain.StorageConfig(domain.StorageKind.IN_MEMORY), None)
    return await build_engines(config, settings, storage, factories)


def anthropic_only(*_: object) -> AgentEngine:
    engine = create_autospec(AgentEngine, spec_set=True, instance=True)
    engine.kinds.return_value = frozenset({ProviderKind.ANTHROPIC})
    return engine


@pytest.mark.parametrize(
    ("engine", "kind", "error", "named"),
    [
        ("other", domain.ProviderKind.ANTHROPIC, domain.UnknownEngineError, "'other'"),
        ("fake", domain.ProviderKind.OPENAI, domain.UnreachableProviderError, "'openai'"),
    ],
)
@asyncio_test
async def test_an_engine_that_cannot_run_the_agent_is_refused_by_name(
    engine: str, kind: domain.ProviderKind, error: type[Exception], named: str
) -> None:
    with pytest.raises(error, match=named):
        await build(config(engine, kind), {"fake": anthropic_only})


@asyncio_test
async def test_a_kataloop_agent_on_openai_itself_is_refused_by_name() -> None:
    # uni-llm-client, kataloop's model client, has no OpenAI kind yet.
    with pytest.raises(domain.UnreachableProviderError, match="engine 'kataloop' cannot reach"):
        await build(config("kataloop", domain.ProviderKind.OPENAI), installed())


@pytest.mark.parametrize(
    ("engine", "built_as", "reaches_openai"),
    [
        ("langchain", "LangChainEngine", True),
        ("pydantic-ai", "PydanticAIEngine", True),
        # uni-llm-client has no OpenAI kind yet.
        ("kataloop", "KataloopEngine", False),
        ("echo", "EchoEngine", True),
    ],
)
@asyncio_test
async def test_an_installed_engine_is_built_by_name(
    engine: str, built_as: str, reaches_openai: bool
) -> None:
    assert set(installed()) == {"langchain", "pydantic-ai", "kataloop", "echo"}
    engines = await build(config(engine), installed())
    assert type(engines[engine]).__name__ == built_as
    assert (ProviderKind.OPENAI in engines[engine].kinds()) is reaches_openai
