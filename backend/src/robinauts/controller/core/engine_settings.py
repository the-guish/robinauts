# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

from __future__ import annotations

from collections.abc import Callable

from robinauts.agent_engines.contract import domain as engine_domain
from robinauts.agent_engines.contract.ports import (
    EngineSettings,
    ProviderKeyLookup,
    StorageConfig,
    StorageKind,
    ToolSecretLookup,
)
from robinauts.controller.contract import domain

SecretLookup = Callable[[str], str | None]
"""An environment variable's value by its name, or ``None``."""


class _Keys(ProviderKeyLookup):
    def __init__(self, config: domain.Config, secret_for: SecretLookup) -> None:
        self._config = config
        self._secret_for = secret_for

    def key_for(self, provider_id: str) -> str:
        provider = self._config.providers.get(provider_id)
        key = self._secret_for(provider.api_key_env) if provider is not None else None
        if not key:
            raise domain.MissingSecretError(f"no key for provider {provider_id!r}")
        return key


class _ToolSecrets(ToolSecretLookup):
    def __init__(self, config: domain.Config, secret_for: SecretLookup) -> None:
        self._config = config
        self._secret_for = secret_for

    def secret_for(self, server_id: str) -> str:
        server = self._config.tool_servers.get(server_id)
        secret = self._secret_for(server.secret_env) if server is not None else None
        if not secret:
            raise domain.MissingSecretError(f"no secret for tool server {server_id!r}")
        return secret


def engine_settings(config: domain.Config, secret_for: SecretLookup) -> EngineSettings:
    """The controller's configuration as the engines take it: the same tables, no agents."""
    models = engine_domain.ModelsConfig(
        providers={
            p.id: engine_domain.ModelProviderConfig(
                p.id, engine_domain.ProviderKind(p.kind.value), p.api_key_env, p.base_url
            )
            for p in config.providers.values()
        },
        models={
            m.id: engine_domain.ModelConfig(
                m.id,
                m.provider,
                m.name,
                m.timeout_seconds,
                m.max_output_tokens,
                m.context_window,
                m.title,
                m.max_retries,
            )
            for m in config.models.values()
        },
        tool_servers={
            t.id: engine_domain.ToolServerConfig(
                t.id,
                t.url,
                t.secret_env,
                engine_domain.ToolServerAuth(t.auth.value),
                t.user,
                t.timeout_seconds,
                t.header,
            )
            for t in config.tool_servers.values()
        },
    )
    limits = engine_domain.RunLimits(
        max_model_calls=config.work.max_model_calls,
        tool_error_behavior=engine_domain.ToolErrorBehavior(config.work.tool_error_behavior.value),
    )
    return EngineSettings(
        models=models,
        keys=_Keys(config, secret_for),
        tool_secrets=_ToolSecrets(config, secret_for),
        limits=limits,
    )


def engine_storage(storage: domain.StorageConfig, pool: object | None) -> StorageConfig:
    """The controller's storage as an engine takes it. A PostgreSQL pool is the controller's."""
    if storage.kind is domain.StorageKind.POSTGRES:
        return StorageConfig(StorageKind.POSTGRES, {"pool": pool})
    if storage.kind is domain.StorageKind.LOCAL:
        return StorageConfig(StorageKind.LOCAL, {"path": storage.path})
    return StorageConfig(StorageKind.IN_MEMORY, {})
