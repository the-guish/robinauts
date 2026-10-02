# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The controller builds the engines its agents name, and refuses what they cannot run."""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator

import pytest

from aio import asyncio_test
from robinauts.agent_engines.contract.domain import AgentDefinition, Event, ProviderKind
from robinauts.agent_engines.contract.ports import (
    AgentEngine,
    EngineSettings,
    StorageConfig,
    StorageKind,
)
from robinauts.controller.application.engines import build_engines
from robinauts.controller.contract import domain
from robinauts.controller.core.engine_settings import engine_settings, engine_storage


class FakeEngine(AgentEngine):
    def __init__(self, settings: EngineSettings, storage: StorageConfig) -> None:
        self.settings = settings
        self.storage = storage
        self.set_up = 0

    def kinds(self) -> frozenset[ProviderKind]:
        return frozenset({ProviderKind.ANTHROPIC})

    async def setup(self) -> None:
        self.set_up += 1

    async def create(self, session_id: uuid.UUID) -> None:
        raise NotImplementedError

    async def exists(self, session_id: uuid.UUID) -> bool:
        raise NotImplementedError

    def stream(
        self,
        session_id: uuid.UUID,
        agent: AgentDefinition,
        prompt: str,
        *,
        model: str,
        checkpoint_id: str | None,
        timeout_seconds: float,
        resume: bool = False,
    ) -> AsyncGenerator[Event, None]:
        raise NotImplementedError

    async def fork(self, source_id: uuid.UUID, target_id: uuid.UUID, *, checkpoint_id: str) -> None:
        raise NotImplementedError

    async def forget(self, session_id: uuid.UUID) -> None:
        raise NotImplementedError


async def init_fake(settings: EngineSettings, storage: StorageConfig) -> AgentEngine:
    return FakeEngine(settings, storage)


FACTORIES = {"fake": init_fake}


def config(kind: domain.ProviderKind = domain.ProviderKind.ANTHROPIC, engine: str = "fake"):
    return domain.Config(
        providers={"acme": domain.ProviderConfig("acme", kind, "ACME_KEY")},
        models={"m": domain.ModelConfig("m", "acme", "model-1")},
        tool_servers={"t": domain.ToolServerConfig("t", "https://tools.example", "T_SECRET")},
        agents={
            "a": domain.AgentConfig("a", "A", "", "m", engine, ("t",)),
            "b": domain.AgentConfig("b", "B", "", "m", engine),
        },
    )


ENV = {"ACME_KEY": "sk-1", "T_SECRET": "s-1"}


@asyncio_test
async def test_one_engine_per_name_built_and_set_up() -> None:
    settings = engine_settings(config(), ENV.get)
    storage = engine_storage(domain.StorageConfig(domain.StorageKind.IN_MEMORY), None)
    engines = await build_engines(config(), settings, storage, FACTORIES)
    assert list(engines) == ["fake"]
    built = engines["fake"]
    assert isinstance(built, FakeEngine)
    assert built.set_up == 1
    assert built.storage.kind is StorageKind.IN_MEMORY


def test_settings_carry_the_tables_without_agents_and_answer_secrets_by_id() -> None:
    settings = engine_settings(config(), ENV.get)
    assert set(settings.models.providers) == {"acme"}
    assert settings.models.providers["acme"].kind is ProviderKind.ANTHROPIC
    assert set(settings.models.models) == {"m"}
    assert set(settings.models.tool_servers) == {"t"}
    assert settings.keys.key_for("acme") == "sk-1"
    assert settings.tool_secrets.secret_for("t") == "s-1"


def test_a_secret_the_environment_lacks_is_refused_by_name() -> None:
    settings = engine_settings(config(), {}.get)
    with pytest.raises(domain.MissingSecretError, match="'acme'"):
        settings.keys.key_for("acme")
    with pytest.raises(domain.MissingSecretError, match="'t'"):
        settings.tool_secrets.secret_for("t")


def test_storage_kinds_map_to_the_engines_options(tmp_path) -> None:
    local = engine_storage(domain.StorageConfig(domain.StorageKind.LOCAL, path=str(tmp_path)), None)
    assert local.kind is StorageKind.LOCAL
    assert local.options == {"path": str(tmp_path)}
    pool = object()
    postgres = engine_storage(domain.StorageConfig(domain.StorageKind.POSTGRES, url="x"), pool)
    assert postgres.kind is StorageKind.POSTGRES
    assert postgres.options == {"pool": pool}


@asyncio_test
async def test_an_engine_this_build_lacks_is_refused_by_name() -> None:
    settings = engine_settings(config(engine="other"), ENV.get)
    with pytest.raises(domain.UnknownEngineError, match="'other'"):
        await build_engines(
            config(engine="other"),
            settings,
            engine_storage(domain.StorageConfig(domain.StorageKind.IN_MEMORY), None),
            FACTORIES,
        )


@asyncio_test
async def test_a_provider_kind_the_engine_cannot_reach_is_refused() -> None:
    settings = engine_settings(config(domain.ProviderKind.OPENAI), ENV.get)
    with pytest.raises(domain.UnreachableProviderError, match="'openai'"):
        await build_engines(
            config(domain.ProviderKind.OPENAI),
            settings,
            engine_storage(domain.StorageConfig(domain.StorageKind.IN_MEMORY), None),
            FACTORIES,
        )


@asyncio_test
async def test_the_installed_engines_are_built_by_name() -> None:
    from robinauts.agent_engines.contract.ports import installed
    from robinauts.agent_engines.echo_engine.engine import EchoEngine
    from robinauts.agent_engines.langchain_engine.engine import LangChainEngine
    from robinauts.agent_engines.pydantic_ai_engine.engine import PydanticAIEngine

    assert set(installed()) == {"langchain", "pydantic-ai", "echo"}
    storage = engine_storage(domain.StorageConfig(domain.StorageKind.IN_MEMORY), None)
    expected = {
        "langchain": LangChainEngine,
        "pydantic-ai": PydanticAIEngine,
        "echo": EchoEngine,
    }
    for name, cls in expected.items():
        settings = engine_settings(config(engine=name), ENV.get)
        engines = await build_engines(config(engine=name), settings, storage, installed())
        assert isinstance(engines[name], cls)
        assert ProviderKind.OPENAI in engines[name].kinds()
