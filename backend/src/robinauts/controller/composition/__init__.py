# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Loads a configuration file, and builds a controller: the store for the storage asked, the
application over it, what a worker needs of the core over that store, and the worker that
claims and runs its turns through it, with the credentials sign-in keeps on the same storage.
A worker in a process of its own is built with `compose_worker`, over the core it is handed.
The file holds web's tables beside the controller's, so web is handed the tables of one
reading and parses its own. Also
what `robinauts db init` does, since it is the one other thing that names the store and the
engines together."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from robinauts.agent_engines.contract.ports import EngineFactory, installed
from robinauts.agent_engines.contract.ports import StorageConfig as EngineStorage
from robinauts.agent_engines.contract.ports import StorageKind as EngineStorageKind
from robinauts.controller.adapters.config_file import read_config
from robinauts.controller.adapters.memory.credentials import MemoryCredentials
from robinauts.controller.adapters.memory.store import MemoryStore
from robinauts.controller.adapters.postgres.credentials import PostgresCredentials
from robinauts.controller.adapters.postgres.engine_memory import PostgresEngineMemory
from robinauts.controller.adapters.postgres.pool import open_pool
from robinauts.controller.adapters.postgres.schema import (
    SCHEMA_SHA256,
    SCHEMA_VERSION,
    create_schema,
)
from robinauts.controller.adapters.postgres.store import PostgresStore
from robinauts.controller.application.controller import RobinautsController
from robinauts.controller.application.turn_broker import InProcessTurnBroker
from robinauts.controller.application.worker import Worker
from robinauts.controller.contract.domain import Config, ConfigError, StorageConfig, StorageKind
from robinauts.controller.contract.ports import Controller, Credentials, TurnBroker
from robinauts.controller.core.config import parse_config
from robinauts.controller.core.engine_settings import SecretLookup, engine_settings
from robinauts.controller.ports.store import Store
from robinauts.tools import sessions
from robinauts.tools.contract.ports import ToolProvider

DATABASE_URL_VARIABLE = "ROBINAUTS_DATABASE_URL"


def built_in_tools() -> dict[str, ToolProvider]:
    """This build's tools by id, loaded from a fixed list: the one place that names a tool."""
    loaded = [sessions.load()]
    return {tool.id: tool for tool in loaded}


CONTROLLER_TABLES = frozenset({"model_providers", "models", "tool_servers", "agents", "work"})
"""The file's tables that are the controller's, the ones `parse_config` reads."""

SCHEMA_READY = "the database is at schema version {version} (schema.sql {digest})"
"""What `db init` says whether it created the schema or found it there: the command's
promise is the state of the database, not the work it did."""


def storage_from(environ: Mapping[str, str]) -> StorageConfig:
    """PostgreSQL when `ROBINAUTS_DATABASE_URL` is set, in memory otherwise."""
    url = environ.get(DATABASE_URL_VARIABLE)
    if url:
        return StorageConfig(StorageKind.POSTGRES, url=url)
    return StorageConfig(StorageKind.IN_MEMORY)


@dataclass(frozen=True, slots=True)
class Composed:
    """The controller, the credentials on its storage, what a worker needs of the core, and
    the worker over it. The credentials and the core open nothing: they use the store, which
    `controller.open` opens and `close` closes. The worker starts after `open` and stops before
    `close`, by the shell's hand; the engines' memory is its own, which it opens at its start
    and closes at its stop."""

    controller: Controller
    credentials: Credentials
    worker: Worker
    broker: TurnBroker
    """Over the controller's store: the worker's in this process, and what the API serves a
    worker in a process of its own."""


def compose(
    config: Config,
    *,
    storage: StorageConfig,
    secret_for: SecretLookup,
    engines: Mapping[str, EngineFactory] | None = None,
) -> Composed:
    """`engines` are the factories by engine name; the installed ones when not given."""
    store: Store
    credentials: Credentials
    if storage.kind is StorageKind.POSTGRES:
        if not storage.url:
            raise ConfigError(f"{DATABASE_URL_VARIABLE} is not set")
        postgres = PostgresStore(dsn=storage.url)
        store, credentials = postgres, PostgresCredentials(postgres)
    elif storage.kind is StorageKind.IN_MEMORY:
        memory = MemoryStore()
        store, credentials = memory, MemoryCredentials(memory)
    else:
        raise NotImplementedError(f"{storage.kind} storage")
    controller = RobinautsController(config, store=store)
    broker = InProcessTurnBroker(store)
    worker = compose_worker(
        config, storage=storage, secret_for=secret_for, broker=broker, engines=engines
    )
    return Composed(controller, credentials, worker, broker)


def compose_worker(
    config: Config,
    *,
    storage: StorageConfig,
    secret_for: SecretLookup,
    broker: TurnBroker,
    engines: Mapping[str, EngineFactory] | None = None,
) -> Worker:
    """A worker that reaches the core through `core` alone. Its engines keep their memory on
    `storage`: on PostgreSQL in a pool of the worker's own, which reads and writes the
    engines' tables, never the core's."""
    engine_memory = None
    if storage.kind is StorageKind.POSTGRES:
        if not storage.url:
            raise ConfigError(f"{DATABASE_URL_VARIABLE} is not set")
        engine_memory = PostgresEngineMemory(storage.url)
    return Worker(
        broker,
        config,
        storage=storage,
        secret_for=secret_for,
        engine_memory=engine_memory,
        engines=engines,
        tools=built_in_tools(),
    )


def build(config: Config, *, storage: StorageConfig, secret_for: SecretLookup) -> Controller:
    return compose(config, storage=storage, secret_for=secret_for).controller


def read_tables(path: Path) -> dict[str, Any]:
    return read_config(path)


def load(path: Path, environ: Mapping[str, str]) -> tuple[Config, SecretLookup]:
    return configure(read_tables(path), environ)


def configure(tables: Mapping[str, Any], environ: Mapping[str, str]) -> tuple[Config, SecretLookup]:
    return parse_config(tables, tools=built_in_tools().keys()), environ.get


async def init_database(url: str, config: Config, secret_for: SecretLookup) -> str:
    """Create the controller's schema in an empty database and set every installed engine
    up, on a pool opened for this one command. Safe to repeat. `ConfigError` for a database
    that is not empty and not this build's."""
    pool = await open_pool(url)
    try:
        await create_schema(pool)
        settings = engine_settings(config, secret_for)
        storage = EngineStorage(EngineStorageKind.POSTGRES, {"pool": pool})
        for factory in installed().values():
            engine = factory(settings, storage)
            await engine.setup()
    finally:
        await pool.close()
    return SCHEMA_READY.format(version=SCHEMA_VERSION, digest=SCHEMA_SHA256[:12])
