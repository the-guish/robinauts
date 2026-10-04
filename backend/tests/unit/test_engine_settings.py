# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The engines' settings and storage, made from the controller's configuration."""

from __future__ import annotations

import dataclasses

import pytest

from robinauts.agent_engines.contract.domain import ProviderKind, ToolServerAuth
from robinauts.agent_engines.contract.ports import StorageKind
from robinauts.controller.contract import domain
from robinauts.controller.core.engine_settings import engine_settings, engine_storage

CONFIG = domain.Config(
    providers={"acme": domain.ProviderConfig("acme", domain.ProviderKind.ANTHROPIC, "ACME_KEY")},
    models={"m": domain.ModelConfig("m", "acme", "model-1")},
    tool_servers={
        "t": domain.ToolServerConfig("t", "https://tools.example", "T_SECRET"),
        "c": domain.ToolServerConfig(
            "c", "https://c.example", "C_KEY", domain.ToolServerAuth.HEADER, header="x-api-key"
        ),
    },
    agents={"a": domain.AgentConfig("a", "A", "", "m", "echo", ("t",))},
)


def test_settings_carry_the_tables_without_agents_and_answer_secrets_by_id() -> None:
    settings = engine_settings(CONFIG, {"ACME_KEY": "sk-1", "T_SECRET": "s-1"}.get)
    assert set(settings.models.providers) == {"acme"}
    assert settings.models.providers["acme"].kind is ProviderKind.ANTHROPIC
    assert set(settings.models.models) == {"m"}
    assert set(settings.models.tool_servers) == {"t", "c"}
    header = settings.models.tool_servers["c"]
    assert (header.auth, header.header) == (ToolServerAuth.HEADER, "x-api-key")
    assert settings.keys.key_for("acme") == "sk-1"
    assert settings.tool_secrets.secret_for("t") == "s-1"


def test_settings_carry_the_bound_on_a_turn_s_model_calls() -> None:
    assert engine_settings(CONFIG, {}.get).max_model_calls_per_turn == 200
    bounded = dataclasses.replace(CONFIG, max_model_calls_per_turn=60)
    assert engine_settings(bounded, {}.get).max_model_calls_per_turn == 60


def test_a_secret_the_environment_lacks_is_refused_by_name() -> None:
    settings = engine_settings(CONFIG, {}.get)
    with pytest.raises(domain.MissingSecretError, match="'acme'"):
        settings.keys.key_for("acme")
    with pytest.raises(domain.MissingSecretError, match="'t'"):
        settings.tool_secrets.secret_for("t")


POOL = object()


@pytest.mark.parametrize(
    ("storage", "kind", "options"),
    [
        (
            domain.StorageConfig(domain.StorageKind.LOCAL, path="/d"),
            StorageKind.LOCAL,
            {"path": "/d"},
        ),
        (
            domain.StorageConfig(domain.StorageKind.POSTGRES, url="x"),
            StorageKind.POSTGRES,
            {"pool": POOL},
        ),
    ],
)
def test_storage_kinds_map_to_the_engines_options(
    storage: domain.StorageConfig, kind: StorageKind, options: dict[str, object]
) -> None:
    mapped = engine_storage(storage, POOL)
    assert (mapped.kind, mapped.options) == (kind, options)
