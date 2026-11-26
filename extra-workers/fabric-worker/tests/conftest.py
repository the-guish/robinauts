# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""What the tests share: the engine's settings for one Anthropic-compatible provider, and the
worker's settings in a temporary folder."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from robinauts.agent_engines.contract.domain import (
    MissingSecretError,
    ModelConfig,
    ModelProviderConfig,
    ModelsConfig,
    ProviderKind,
    ToolServerAuth,
    ToolServerConfig,
)
from robinauts.agent_engines.contract.ports import (
    EngineSettings,
    ProviderKeyLookup,
    ToolSecretLookup,
)
from robinauts_fabric_worker.settings import Settings, settings_from

KEY = "sk-test-not-a-key"
TOOL_SECRET = "tool-secret-value"
MODEL = "local"


class Keys(ProviderKeyLookup):
    def key_for(self, provider_id: str) -> str:
        if provider_id != "local":
            raise MissingSecretError(provider_id)
        return KEY


class ToolSecrets(ToolSecretLookup):
    def secret_for(self, server_id: str) -> str:
        return TOOL_SECRET


def engine_settings(base_url: str = "http://127.0.0.1:9") -> EngineSettings:
    return EngineSettings(
        models=ModelsConfig(
            providers={
                "local": ModelProviderConfig(
                    "local", ProviderKind.ANTHROPIC_COMPATIBLE, "LOCAL_KEY", base_url
                ),
                "openai": ModelProviderConfig("openai", ProviderKind.OPENAI, "OPENAI_KEY"),
            },
            models={
                MODEL: ModelConfig(MODEL, "local", "claude-sonnet-5-5"),
                "other": ModelConfig("other", "local", "claude-opus-5-5"),
                "gpt": ModelConfig("gpt", "openai", "gpt-6"),
            },
            tool_servers={
                "docs": ToolServerConfig(
                    "docs", "https://docs.example/mcp", "DOCS_SECRET", exclude=("delete",)
                ),
                "wiki": ToolServerConfig(
                    "wiki", "https://wiki.example/mcp", "WIKI_SECRET", ToolServerAuth.BASIC, "me"
                ),
                "open": ToolServerConfig(
                    "open", "https://open.example/mcp", "", ToolServerAuth.NONE
                ),
            },
        ),
        keys=Keys(),
        tool_secrets=ToolSecrets(),
    )


@pytest.fixture
def worker(tmp_path: Path) -> Settings:
    settings = settings_from({})
    return replace(settings, workspaces=tmp_path / "workspaces", state=tmp_path / "state")
