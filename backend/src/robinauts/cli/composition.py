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
engines together. And the orchestrator, when ``ROBINAUTS_FLOWS_DIR`` names a flows folder:
neorc, embedded (``orchestrator/adapters/neorc``)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
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
from robinauts.orchestrator.adapters.flows_folder import read_forms, read_settings
from robinauts.orchestrator.adapters.neorc import embedded as neorc
from robinauts.orchestrator.application.flows import FlowForms
from robinauts.orchestrator.contract.domain import FLOWS_VARIABLE, FormSpec, Orchestration
from robinauts.orchestrator.core.config import parse_flows_settings
from robinauts.orchestrator.core.forms import check_forms, parse_form
from robinauts.shared_domain.domain import DEFAULT_WORKER, Config, StorageConfig, StorageKind
from robinauts.tools import sessions
from robinauts.tools.contract.ports import ToolProvider
from robinauts.worker.agent_engine_store import PoolAgentEngineStore
from robinauts.worker.engine_settings import SecretLookup, engine_settings
from robinauts.worker.http_turn_broker import HttpTurnBroker
from robinauts.worker.worker import Worker

DATABASE_URL_VARIABLE = "ROBINAUTS_DATABASE_URL"


def built_in_tools() -> dict[str, ToolProvider]:
    """This build's tools by id, loaded from a fixed list: the one place that names a tool."""
    loaded = [sessions.load()]
    return {tool.id: tool for tool in loaded}


CONTROLLER_TABLES = frozenset(
    {"model_providers", "models", "tool_servers", "agents", "workers", "work"}
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
    commands: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    """By worker, the command that starts the process of those whose table names one
    (``Config.commands``)."""
    config: Config = field(default_factory=Config)
    """What it was composed from: web reads the agents' tool servers in it, for their apps."""


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
    return Composed(controller, credentials, workers, brokers, config.commands, config)


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


def orchestrator_of(
    environ: Mapping[str, str],
    config: Config,
    *,
    default_code: Path,
    api_url: str,
) -> Orchestration | None:
    """neorc over the flows folder ``ROBINAUTS_FLOWS_DIR`` names, as its ``flows.toml``
    describes it, on this server's storage, and where its worker reaches this server; and the
    folder's flows as forms, with the lists of values ``config``, the agents' configuration,
    and the folder's repositories give. ``None`` without the variable. ``default_code`` is
    where the handlers are when ``flows.toml`` names none. ``ConfigError`` names what stops it
    from starting."""
    named = environ.get(FLOWS_VARIABLE)
    if not named:
        return None
    folder = Path(named).absolute()
    problems: list[str] = []
    forms: dict[str, FormSpec] = {}
    declared: dict[str, dict[str, str]] = {}
    settings: dict[str, Any] = {}
    if folder.is_dir():
        settings = read_settings(folder)
        for flow, table in read_forms(folder).items():
            form, wrong = parse_form(flow, table)
            problems += wrong
            if form is not None:
                forms[flow] = form
        declared = neorc.declared_inputs(folder)
        problems += check_forms(forms, declared)
    try:
        described = parse_flows_settings(
            settings, folder=folder, default_code=default_code, forms=forms
        )
    except ConfigError as refused:
        problems.insert(0, str(refused))
    else:
        command = neorc.neorc_command()
        ui = neorc.built_ui()
        problems = neorc.check_ready(described, command=command, ui=ui) + problems
    if problems:
        raise ConfigError("\n".join(problems))
    embedded = neorc.EmbeddedNeorc(
        described,
        environ,
        hidden=neorc.hidden_variables(config),
        api_url=api_url,
        database_url=environ.get(DATABASE_URL_VARIABLE) or None,
        command=command,
        ui=ui,
    )
    return Orchestration(
        embedded=embedded, flows=FlowForms(embedded.engine, described, declared, config)
    )
