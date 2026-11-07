# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The one module that imports both the controller and the worker; the rest of ``cli`` gets
them from here. Loads a configuration file, and builds a controller: the store for the storage
asked, the application over it, and for each worker the configuration names, what it needs of
the core over that store and the worker that claims and runs its agents' turns through it, with
the credentials sign-in keeps on the same storage.
A worker in a process of its own is built with `compose_worker`, over the core it is handed:
`http_turn_broker`, the server's API.
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
from robinauts.controller.adapters.in_process.credentials import InProcessCredentials
from robinauts.controller.adapters.in_process.store import InProcessStore
from robinauts.controller.adapters.postgres.credentials import PostgresCredentials
from robinauts.controller.adapters.postgres.pool import close_pool, open_pool
from robinauts.controller.adapters.postgres.schema import (
    SCHEMA_SHA256,
    SCHEMA_VERSION,
    create_schema,
)
from robinauts.controller.adapters.postgres.store import PostgresStore
from robinauts.controller.application.controller import RobinautsController
from robinauts.controller.application.turn_broker import InProcessTurnBroker
from robinauts.controller.contract.broker.ports import TurnBroker
from robinauts.controller.contract.domain import ConfigError
from robinauts.controller.contract.ports import Controller, Credentials
from robinauts.controller.core.config import parse_config
from robinauts.controller.ports.store import Store
from robinauts.filesystems.contract.domain import MAX_SEGMENT_BYTES, plain
from robinauts.filesystems.contract.ports import FileSystem
from robinauts.filesystems.local import LocalFileSystem
from robinauts.shared_domain.domain import (
    DEFAULT_WORKER,
    Config,
    FilesKind,
    StorageConfig,
    StorageKind,
)
from robinauts.tools import basic_edit, sessions
from robinauts.tools.contract.ports import ToolProvider
from robinauts.worker.agent_engine_store import PoolAgentEngineStore
from robinauts.worker.engine_settings import SecretLookup, engine_settings
from robinauts.worker.http_turn_broker import HttpTurnBroker
from robinauts.worker.worker import Worker

DATABASE_URL_VARIABLE = "ROBINAUTS_DATABASE_URL"


def built_in_tools() -> dict[str, ToolProvider]:
    """This build's tools by id, loaded from a fixed list: the one place that names a tool."""
    loaded = [sessions.load(), basic_edit.load()]
    return {tool.id: tool for tool in loaded}


KEEPS_FILES = frozenset({basic_edit.ID})
"""The tools of this build that keep files in the owner's folder with the agent: an agent
that names one needs a file system, and an id that can name a folder."""


def keeps_files(config: Config) -> bool:
    """Whether any agent names a tool that keeps files. `ConfigError` for one whose id cannot
    name a folder, said at start-up rather than at its first turn."""
    using = [agent for agent in config.agents.values() if KEEPS_FILES & set(agent.tools)]
    if wrong := [agent.id for agent in using if not plain(agent.id)]:
        tools = ", ".join(sorted(KEEPS_FILES))
        raise ConfigError(
            "\n".join(
                f"agents.{id_}: an agent with the {tools} tool keeps files in a folder named"
                f" after it, and {id_!r} cannot name one: no '/' or backslash, not '.' or"
                f" '..', printable, at most {MAX_SEGMENT_BYTES} bytes"
                for id_ in wrong
            )
        )
    return bool(using)


def file_system_for(config: Config) -> FileSystem | None:
    """The file system the agents' folders are on, of the kind `[files]` names; none when no
    agent keeps files."""
    if not keeps_files(config):
        return None
    if config.files.kind is FilesKind.LOCAL:
        return LocalFileSystem(Path(config.files.path).expanduser())
    raise NotImplementedError(f"{config.files.kind} files")


CONTROLLER_TABLES = frozenset(
    {"model_providers", "models", "tool_servers", "agents", "work", "files"}
)
"""The file's tables that are the controller's, the ones `parse_config` reads."""

SCHEMA_READY = "the database is at schema version {version} (schema.sql {digest})"
"""What `db init` says whether it created the schema or found it there: the command's
promise is the state of the database, not the work it did."""


def storage_from(environ: Mapping[str, str]) -> StorageConfig:
    """PostgreSQL when `ROBINAUTS_DATABASE_URL` is set, in this process otherwise."""
    url = environ.get(DATABASE_URL_VARIABLE)
    if url:
        return StorageConfig(StorageKind.POSTGRES, url=url)
    return StorageConfig(StorageKind.IN_PROCESS)


@dataclass(frozen=True, slots=True)
class Composed:
    """The controller, the credentials on its storage, and for each worker by name what it
    needs of the core and the worker over it. The credentials and the core open nothing: they
    use the store, which `controller.open` opens and `close` closes. The workers start after
    `open` and stop before `close`, by the shell's hand; the agent engines' store is each
    worker's own, which it opens at its start and closes at its stop."""

    controller: Controller
    credentials: Credentials
    workers: Mapping[str, Worker]
    """By name, the default first: those that run in this process."""
    brokers: Mapping[str, TurnBroker]
    """By worker, over the controller's store: each worker's in this process, and what the API
    serves the worker of that name in a process of its own."""


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
    elif storage.kind is StorageKind.IN_PROCESS:
        in_process = InProcessStore()
        store, credentials = in_process, InProcessCredentials(in_process)
    else:
        raise NotImplementedError(f"{storage.kind} storage")
    controller = RobinautsController(config, store=store)
    brokers: dict[str, TurnBroker] = {}
    workers: dict[str, Worker] = {}
    for name in config.workers():
        brokers[name] = InProcessTurnBroker(store, name)
        workers[name] = compose_worker(
            config,
            storage=storage,
            secret_for=secret_for,
            broker=brokers[name],
            engines=engines,
            name=name,
        )
    return Composed(controller, credentials, workers, brokers)


def compose_worker(
    config: Config,
    *,
    storage: StorageConfig,
    secret_for: SecretLookup,
    broker: TurnBroker,
    engines: Mapping[str, EngineFactory] | None = None,
    name: str = DEFAULT_WORKER,
) -> Worker:
    """The worker `name`, which reaches the core through `broker` alone. Its engines keep their
    state on `storage`: on PostgreSQL in a pool of the worker's own, which reads and writes the
    engines' tables, never the core's."""
    agent_engine_store = None
    if storage.kind is StorageKind.POSTGRES:
        if not storage.url:
            raise ConfigError(f"{DATABASE_URL_VARIABLE} is not set")
        url = storage.url
        agent_engine_store = PoolAgentEngineStore(lambda: open_pool(url), close_pool)
    return Worker(
        broker,
        config,
        storage=storage,
        secret_for=secret_for,
        agent_engine_store=agent_engine_store,
        engines=engines,
        tools=built_in_tools(),
        files=file_system_for(config),
        name=name,
    )


def http_turn_broker(url: str, secret: str) -> TurnBroker:
    """What a worker in a process of its own reaches the core through: the API of the server
    at `url`, with the secret that server handed it."""
    return HttpTurnBroker(url, secret)


def build(config: Config, *, storage: StorageConfig, secret_for: SecretLookup) -> Controller:
    return compose(config, storage=storage, secret_for=secret_for).controller


def read_tables(path: Path) -> dict[str, Any]:
    return read_config(path)


def load(path: Path, environ: Mapping[str, str]) -> tuple[Config, SecretLookup]:
    return configure(read_tables(path), environ)


def configure(tables: Mapping[str, Any], environ: Mapping[str, str]) -> tuple[Config, SecretLookup]:
    config = parse_config(tables, tools=built_in_tools().keys())
    keeps_files(config)
    return config, environ.get


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
