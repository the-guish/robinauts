# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Robinauts as a part of a backend: its own component and neorc's manager, wired, for whoever
composes them with oneberth (``docs/architecture/web.md``, "Robinauts as a part").

``robinauts_parts`` is the one call. It reads the environment as ``robinauts start`` does: the
configuration ``ROBINAUTS_CONFIG`` names, the sign-in, the storage and the flows folder. It
returns the ``Parts``: the two components, and what oneberth composes them with. ``robinauts
start`` composes them alone (``cli/main.py``). A host with applications of its own composes
them beside its own::

    parts = robinauts_parts(os.environ, front=front, worker=worker, local=True)
    composition = compose_app(
        [*parts.components, mine],
        config=parts.oneberth,
        store=parts.store,
        interface=built_interface,
        local=parts.local,
    )
    parts.composed_into(composition)
    run(composition, front, worker)
"""

from __future__ import annotations

import sys
import tomllib
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from oneberth.config import Config as OneberthConfig
from oneberth.config import ConfigError as OneberthConfigError
from oneberth.config import is_loopback, load_config, local_config
from oneberth.identity import Access, IdentityStore
from oneberth.identity.memory import MemoryIdentityStore
from oneberth.stores import opened_store
from oneberth.web import Component, Composition, Context, Listen

from robinauts.cli.composition import (
    CONTROLLER_TABLES,
    DATA_DIR_VARIABLE,
    DATABASE_URL_VARIABLE,
    Composed,
    SecretLookup,
    compose,
    configure,
    read_tables,
    storage_from,
)
from robinauts.cli.lifecycle import Lifecycle
from robinauts.cli.orchestration import CONFIG_VARIABLE, orchestrator_of
from robinauts.controller.contract.domain import ConfigError
from robinauts.orchestrator.contract.domain import MOVED, TABLE, Orchestrator
from robinauts.shared_domain.domain import Config, StorageKind
from robinauts.web.app import NAME, new_robinauts_webapp
from robinauts.web.roles import WORKER_ROLE

REPO = Path(__file__).resolve().parents[4]
DEFAULT_CODE = REPO / "orchestration"
"""Where the handlers Robinauts ships are, when the flows' settings name no other place."""

COMMAND = (sys.executable, "-m", "robinauts.cli")
"""This build's ``robinauts`` command, as this interpreter runs it."""

ONEBERTH_CONFIG_VARIABLE = "ONEBERTH_CONFIG"

SIGN_IN_KEYS = frozenset(
    {"public_url", "session_hours", "device_days", "providers", "allow", "admin"}
)
"""The keys Robinauts' file held for sign-in before oneberth held it: refused with ``MOVED_TO``."""
MOVED_TO = (
    "{}: sign-in is oneberth's: its configuration is the file ONEBERTH_CONFIG names, with the"
    " backend's public_url under [apps.robinauts]"
)
NO_PROVIDER = (
    "no identity provider is configured: name one in the file ONEBERTH_CONFIG names, or start"
    " with --local, oneberth's local mode, for the one person at this machine"
)
NO_ONEBERTH_CONFIG = (
    f"{ONEBERTH_CONFIG_VARIABLE} names no file: it holds the sign-in, or start with --local"
)
LOCAL_HAS_NO_FILE = (
    f"--local signs the one person at this machine in with a ticket: unset"
    f" {ONEBERTH_CONFIG_VARIABLE}"
)
NO_DATABASE = (
    f"no database: set {DATABASE_URL_VARIABLE} to the PostgreSQL this deployment uses, or"
    f" {DATA_DIR_VARIABLE} to its data directory"
)
TWO_STORAGES = f"{DATABASE_URL_VARIABLE} and {DATA_DIR_VARIABLE} are both set: set one"
IN_A_CONTAINER = (
    "workers.{}: runs in a container of its image, which robinauts desktop starts; a server"
    " starts a worker with the `command` its table names"
)
NO_CONFIG = (
    f"{CONFIG_VARIABLE} names no file: it holds the models, the agents, the tool servers and"
    " the workers"
)
NOT_COMPOSED = (
    "Robinauts' parts reach the backend they are composed into, and were told of none: call"
    " Parts.composed_into with the composition"
)


def tables_of(environ: Mapping[str, str]) -> dict[str, Any]:
    """The tables of the file ``ROBINAUTS_CONFIG`` names. ``ConfigError`` for a variable that
    names none, a file that cannot be read, and a file that is not TOML."""
    named = environ.get(CONFIG_VARIABLE)
    if not named:
        raise ConfigError(NO_CONFIG)
    try:
        return read_tables(Path(named))
    except OSError as unreadable:
        raise ConfigError(f"{CONFIG_VARIABLE}: {named}: {unreadable.strerror}") from unreadable
    except tomllib.TOMLDecodeError as malformed:
        raise ConfigError(f"{CONFIG_VARIABLE}: {named}: {malformed}") from malformed


def serving(
    tables: Mapping[str, Any],
    environ: Mapping[str, str],
    *,
    host: str,
    port: int,
    local: bool,
) -> tuple[Config, SecretLookup, OneberthConfig]:
    """What ``start`` serves with: the controller's configuration, how a secret is read, and
    oneberth's configuration: the file ``ONEBERTH_CONFIG`` names, or the local mode's.
    ``ConfigError`` names every problem of a start that is refused."""
    problems = []
    for key in tables:
        if key == TABLE:
            problems.append(MOVED)
        elif key in SIGN_IN_KEYS:
            problems.append(MOVED_TO.format(key))
        elif key not in CONTROLLER_TABLES:
            problems.append(f"{key}: unknown key, not a table of the controller's")
    if environ.get(DATABASE_URL_VARIABLE) and environ.get(DATA_DIR_VARIABLE):
        problems.append(TWO_STORAGES)
    oneberth = OneberthConfig()
    if local:
        if environ.get(ONEBERTH_CONFIG_VARIABLE):
            problems.append(LOCAL_HAS_NO_FILE)
        if not is_loopback(host):
            problems.append(
                f"--local serves this machine alone: {host} is not a loopback address or"
                " localhost. Bind elsewhere with --host, and name the loopback address a browser"
                " reaches with --public-host"
            )
        else:
            oneberth = local_config(NAME, port=port, host=host)
    else:
        if not environ.get(ONEBERTH_CONFIG_VARIABLE):
            problems.append(NO_ONEBERTH_CONFIG)
        else:
            try:
                oneberth = load_config(environ=environ)
                oneberth.app(NAME)
            except OneberthConfigError as refused:
                problems += refused.problems
            else:
                if not oneberth.providers:
                    problems.append(NO_PROVIDER)
        if not environ.get(DATABASE_URL_VARIABLE) and not environ.get(DATA_DIR_VARIABLE):
            problems.append(NO_DATABASE)
    try:
        config, secret_for = configure(tables, environ)
    except ConfigError as refused:
        problems.append(str(refused))
    if problems:
        raise ConfigError("\n".join(problems))
    return config, secret_for, oneberth


def local_url(host: str, port: int) -> str:
    """Where a process on this machine reaches a listener on ``host``: a loopback address for a
    listener on every address, else that host."""
    if host in ("", "0.0.0.0"):
        host = "127.0.0.1"
    elif host == "::":
        host = "::1"
    if ":" in host:
        host = f"[{host}]"
    return f"http://{host}:{port}"


def store_of(
    environ: Mapping[str, str],
) -> Callable[[], AbstractAsyncContextManager[Any]]:
    """What opens oneberth's store for the life of the backend: the schema ``oneberth`` of the
    PostgreSQL, which ``robinauts store init`` makes; the file ``oneberth.db`` in the data
    directory, which the backend makes; or a store in this process."""
    url = environ.get(DATABASE_URL_VARIABLE)
    folder = environ.get(DATA_DIR_VARIABLE)
    if url:
        return lambda: opened_store(database_url=url)
    if folder:
        Path(folder).mkdir(parents=True, exist_ok=True)
        return lambda: opened_store(data_dir=folder, setup=True)

    @asynccontextmanager
    async def in_memory() -> AsyncIterator[IdentityStore]:
        store = MemoryIdentityStore()
        await store.setup()
        yield store

    return in_memory


class LateTransport(httpx.AsyncBaseTransport):
    """An httpx transport to an ASGI application named after it is built: how a part of the
    backend reaches the backend's own front listener, in its own process."""

    def __init__(self) -> None:
        self.app: Any = None

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if self.app is None:
            raise RuntimeError(NOT_COMPOSED)
        return await httpx.ASGITransport(app=self.app).handle_async_request(request)


class Parts:
    """Robinauts in a backend: its own component and neorc's manager, the lifecycle of the
    controller and the workers, and neorc's start. A host composes ``components`` with
    ``oneberth``, the sign-in's configuration, on ``store``, oneberth's store, in the ``local``
    mode or not. It then tells the parts into what: ``composed_into``.

    Robinauts' component starts first and stops last. Its lifespan opens neorc's storage and
    the controller, and starts the workers. Its serving begins once both listeners serve: it
    uploads the flows and starts neorc's worker, which call the backend's own listeners. It
    ends while the listeners still serve, and stops every worker. The tokens the backend mints
    for its own parts live in its memory alone, and end with it."""

    def __init__(
        self,
        composed: Composed,
        lifecycle: Lifecycle,
        orchestrator: Orchestrator,
        *,
        oneberth: OneberthConfig,
        secret_for: SecretLookup,
        store: Callable[[], AbstractAsyncContextManager[Any]] | IdentityStore,
        local: bool = False,
    ) -> None:
        self.composed = composed
        self.lifecycle = lifecycle
        self.orchestrator = orchestrator
        self.oneberth = oneberth
        self.store = store
        self.local = local
        self.transport = LateTransport()
        self._access: Access | None = None
        self.component = new_robinauts_webapp(
            composed.controller,
            composed.brokers,
            worker_access=lifecycle.access,
            feed=lifecycle.feed,
            config=composed.config,
            secret_for=secret_for,
            lifespan=self._lifespan,
            serving=self._serving,
            health=self._healthy,
            flows=orchestrator.flows(self.transport),
            starter_of=orchestrator.starter_of,
        )

    @property
    def components(self) -> tuple[Component, Component]:
        """Robinauts' component, then neorc's: the order they are composed in."""
        neorc = self.orchestrator.component
        assert isinstance(neorc, Component)
        return (self.component, neorc)

    def composed_into(self, composition: Composition) -> None:
        """Name the backend the components are composed into. The parts reach its front
        listener in its own process: the flows' upload, and a run's start."""
        self.transport.app = composition.front

    async def _healthy(self) -> bool:
        return self.lifecycle.healthy()

    async def _mint(self, name: str, role: str, resource: str | None) -> str:
        """A token of this backend's own, in its memory: it ends when the backend stops."""
        assert self._access is not None, "the backend is not started"
        secret, _ = await self._access.create_own_token(name, role, resource=resource)
        return secret

    @asynccontextmanager
    async def _lifespan(self, context: Context) -> AsyncIterator[None]:
        """Open neorc's storage, then the controller, then start the workers; and the
        reverse at the stop. neorc's component starts after this, and stops before."""
        self._access = context.access
        await self.orchestrator.open()
        try:
            await self.lifecycle.start(
                lambda name: self._mint(f"robinauts-worker-{name}", WORKER_ROLE, name)
            )
            try:
                yield
            finally:
                # The serving stopped them, unless it never began.
                self.lifecycle.feed.close()
                try:
                    await self.orchestrator.stop()
                finally:
                    await self.lifecycle.stop()
        finally:
            await self.orchestrator.close()

    @asynccontextmanager
    async def _serving(self, context: Context) -> AsyncIterator[None]:
        """Once both listeners serve: upload the flows and start neorc's worker. At the end,
        while the listeners still serve, stop the workers: one in a process of its own ends
        its turns through them. The change feed's polls end first."""
        await self.orchestrator.start(self._mint, self.transport)
        try:
            yield
        finally:
            self.lifecycle.feed.close()
            try:
                await self.orchestrator.stop()
            finally:
                await self.lifecycle.stop_worker()


def robinauts_parts(
    environ: Mapping[str, str],
    *,
    front: Listen,
    worker: Listen,
    local: bool,
    public_host: str | None = None,
    supervised: bool = False,
) -> Parts:
    """Robinauts' parts, as ``environ`` configures them, for a backend whose front listener is
    ``front`` and whose worker listener is ``worker``. ``local`` is oneberth's local mode.
    ``public_host`` is where a browser reaches the front listener, its own host by default. A
    ``supervised`` backend starts no worker: its supervisor, the launcher, starts every one.
    ``ConfigError`` names every problem of a configuration that is refused, and a configuration
    that is not there or cannot be read."""
    public_host = front.host if public_host is None else public_host
    tables = tables_of(environ)
    config, secret_for, oneberth = serving(
        tables, environ, host=public_host, port=front.port, local=local
    )
    worker_url = local_url(worker.host, worker.port)
    orchestrator = orchestrator_of(
        environ,
        config,
        default_code=DEFAULT_CODE,
        public_url=oneberth.app(NAME).public_url,
        api_url=local_url(front.host, front.port),
        worker_url=worker_url,
        program=(*COMMAND, "neorc-worker"),
        supervised=supervised,
    )
    if not supervised and (in_a_container := sorted(set(config.containers) - set(config.commands))):
        raise ConfigError("\n".join(IN_A_CONTAINER.format(name) for name in in_a_container))
    storage = storage_from(environ)
    composed = compose(config, storage=storage, secret_for=secret_for)
    lifecycle = Lifecycle(
        composed,
        broker_url=f"{worker_url}/{NAME}",
        # A store in this process is this process's alone, and so is a SQLite file: its workers
        # run here, but for one that starts with a command of its own.
        in_process=storage.kind is not StorageKind.POSTGRES,
        outside=composed.workers if supervised else config.externals,
    )
    return Parts(
        composed,
        lifecycle,
        orchestrator,
        oneberth=oneberth,
        secret_for=secret_for,
        store=store_of(environ),
        local=local,
    )
