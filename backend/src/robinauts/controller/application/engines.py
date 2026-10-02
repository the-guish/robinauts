# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""How the controller builds the engines its agents name, from the ones this build has.

The controller names no engine: it is handed the engines this build has, by name, and
the configuration says which to build.
"""

from __future__ import annotations

from collections.abc import Mapping

from robinauts.agent_engines.contract import domain as engine_domain
from robinauts.agent_engines.contract.ports import (
    AgentEngine,
    EngineFactory,
    EngineSettings,
    StorageConfig,
)
from robinauts.controller.contract import domain


async def build_engines(
    config: domain.Config,
    settings: EngineSettings,
    storage: StorageConfig,
    factories: Mapping[str, EngineFactory],
    *,
    setup: bool = True,
) -> dict[str, AgentEngine]:
    """The engines the agents name, built, set up, and able to reach their models' providers.

    ``setup`` is false on PostgreSQL, where ``robinauts db init`` set the engines up and the
    server never changes the database. ``UnknownEngineError`` for an engine this build does
    not have, ``UnreachableProviderError`` for an agent whose model is on a provider kind its
    engine cannot reach.
    """
    engines: dict[str, AgentEngine] = {}
    for agent in config.agents.values():
        if agent.engine in engines:
            continue
        factory = factories.get(agent.engine)
        if factory is None:
            raise domain.UnknownEngineError(
                f"agent {agent.id!r} runs on engine {agent.engine!r}, which this build"
                f" does not have"
            )
        engine = await factory(settings, storage)
        if setup:
            await engine.setup()
        engines[agent.engine] = engine
    for agent in config.agents.values():
        model = config.models[agent.model]
        kind = config.providers[model.provider].kind
        if engine_domain.ProviderKind(kind.value) not in engines[agent.engine].kinds():
            raise domain.UnreachableProviderError(
                f"agent {agent.id!r} runs on model {agent.model!r}, of kind {kind.value!r},"
                f" which engine {agent.engine!r} cannot reach"
            )
    return engines
