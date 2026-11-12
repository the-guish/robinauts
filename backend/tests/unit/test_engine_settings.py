# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The engines' settings and storage, made from the controller's configuration."""

from __future__ import annotations

import pytest

from robinauts.agent_engines.contract.domain import ProviderKind, ToolServerAuth
from robinauts.agent_engines.contract.ports import StorageKind
from robinauts.shared_domain import domain as shared
from robinauts.worker.engine_settings import engine_settings, engine_storage

CONFIG = shared.Config(
    providers={"acme": shared.ProviderConfig("acme", shared.ProviderKind.ANTHROPIC, "ACME_KEY")},
    models={"m": shared.ModelConfig("m", "acme", "model-1")},
    tool_servers={
        "t": shared.ToolServerConfig("t", "https://tools.example", "T_SECRET"),
        "c": shared.ToolServerConfig(
            "c", "https://c.example", "C_KEY", shared.ToolServerAuth.HEADER, header="x-api-key"
        ),
    },
    agents={"a": shared.AgentConfig("a", "A", "", ("m",), "echo", ("t",))},
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


def test_a_secret_the_environment_lacks_is_refused_by_name() -> None:
    settings = engine_settings(CONFIG, {}.get)
    with pytest.raises(shared.MissingSecretError, match="'acme'"):
        settings.keys.key_for("acme")
    with pytest.raises(shared.MissingSecretError, match="'t'"):
        settings.tool_secrets.secret_for("t")


POOL = object()


@pytest.mark.parametrize(
    ("storage", "kind", "options"),
    [
        (
            shared.StorageConfig(shared.StorageKind.LOCAL, path="/d"),
            StorageKind.LOCAL,
            {"path": "/d"},
        ),
        (
            shared.StorageConfig(shared.StorageKind.POSTGRES, url="x"),
            StorageKind.POSTGRES,
            {"pool": POOL},
        ),
    ],
)
def test_storage_kinds_map_to_the_engines_options(
    storage: shared.StorageConfig, kind: StorageKind, options: dict[str, object]
) -> None:
    mapped = engine_storage(storage, POOL)
    assert (mapped.kind, mapped.options) == (kind, options)
