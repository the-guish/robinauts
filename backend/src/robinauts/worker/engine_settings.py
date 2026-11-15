# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from robinauts.agent_engines.contract import domain as engine_domain
from robinauts.agent_engines.contract.ports import (
    EngineSettings,
    ProviderKeyLookup,
    StorageConfig,
    StorageKind,
    ToolSecretLookup,
)
from robinauts.shared_domain import domain as shared

SecretLookup = Callable[[str], str | None]
"""An environment variable's value by its name, or ``None``."""


class _Keys(ProviderKeyLookup):
    def __init__(self, config: shared.Config, secret_for: SecretLookup) -> None:
        self._config = config
        self._secret_for = secret_for

    def key_for(self, provider_id: str) -> str:
        provider = self._config.providers.get(provider_id)
        key = self._secret_for(provider.api_key_env) if provider is not None else None
        if not key:
            raise shared.MissingSecretError(f"no key for provider {provider_id!r}")
        return key


class _ToolSecrets(ToolSecretLookup):
    def __init__(self, config: shared.Config, secret_for: SecretLookup) -> None:
        self._config = config
        self._secret_for = secret_for

    def secret_for(self, server_id: str) -> str:
        server = self._config.tool_servers.get(server_id)
        secret = self._secret_for(server.secret_env) if server is not None else None
        if not secret:
            raise shared.MissingSecretError(f"no secret for tool server {server_id!r}")
        return secret


def engine_settings(config: shared.Config, secret_for: SecretLookup) -> EngineSettings:
    """The configuration as the engines take it: the same tables, no agents."""
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
                t.exclude,
            )
            for t in config.tool_servers.values()
        },
    )
    return EngineSettings(
        models=models, keys=_Keys(config, secret_for), tool_secrets=_ToolSecrets(config, secret_for)
    )


WORKERS_FOLDER = "workers"
"""The data directory's folder that holds a folder per worker, for its engines' files."""


def worker_folder(data_dir: str | Path, worker: str) -> Path:
    """Where the engines of ``worker`` keep their files on local storage: a folder named after
    the worker, which a worker name makes safe, in ``WORKERS_FOLDER`` of the data directory."""
    return Path(data_dir) / WORKERS_FOLDER / worker


def engine_storage(
    storage: shared.StorageConfig, pool: object | None, worker: str
) -> StorageConfig:
    """The storage asked for, as the engines of ``worker`` take it. A PostgreSQL pool is the
    worker's own (``AgentEngineStore``), for the engines' tables, never the core's. Local
    storage is the worker's own folder in the data directory."""
    if storage.kind is shared.StorageKind.POSTGRES:
        return StorageConfig(StorageKind.POSTGRES, {"pool": pool})
    if storage.kind is shared.StorageKind.LOCAL:
        assert storage.path is not None
        return StorageConfig(StorageKind.LOCAL, {"path": worker_folder(storage.path, worker)})
    return StorageConfig(StorageKind.IN_PROCESS, {})
