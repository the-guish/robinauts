# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Engine settings for the tests of both engines: a made-up key per provider, no tool secrets."""

from __future__ import annotations

from robinauts.agent_engines.contract.domain import (
    ModelConfig,
    ModelProviderConfig,
    ModelsConfig,
    ProviderKind,
)
from robinauts.agent_engines.contract.ports import (
    EngineSettings,
    ProviderKeyLookup,
    ToolSecretLookup,
)


class Keys(ProviderKeyLookup):
    def key_for(self, provider_id: str) -> str:
        return f"key-of-{provider_id}"


class NoSecrets(ToolSecretLookup):
    def secret_for(self, server_id: str) -> str:
        raise AssertionError(server_id)


def settings_for(kind: ProviderKind, base_url: str | None = None) -> EngineSettings:
    """One provider ``p`` of that kind and one model ``m`` on it, with a timeout, a limit and
    retries."""
    provider = ModelProviderConfig(id="p", kind=kind, api_key_env="", base_url=base_url)
    model = ModelConfig(
        id="m",
        provider="p",
        name="vendor-name",
        timeout_seconds=7.0,
        max_output_tokens=321,
        max_retries=3,
    )
    return EngineSettings(
        models=ModelsConfig(providers={"p": provider}, models={"m": model}),
        keys=Keys(),
        tool_secrets=NoSecrets(),
    )
