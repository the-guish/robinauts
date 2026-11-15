# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The ``robinauts`` command: the one place that runs Robinauts, and that starts and stops its
parts in order. ``robinauts start`` serves the backend; ``robinauts db init`` makes Robinauts'
own tables ready on PostgreSQL; ``robinauts version`` prints the version. ``store``,
``tokens``, ``sessions`` and ``devices`` are oneberth's admin scripts, with Robinauts' and
neorc's roles (oneberth's ``docs/specs/identity.md``, "Admin scripts").

The backend is a oneberth composition of two components: Robinauts' own, served under
``/robinauts/``, and neorc's manager, under ``/neorc/`` (``docs/architecture/web.md``). oneberth
serves ``/auth``, the interface under ``/ui/`` and ``/health``, on the front listener
(``--host``, ``--port``), and the workers' routes on the worker listener (``--worker-host``,
``--worker-port``).

Two files configure it. ``ROBINAUTS_CONFIG`` names Robinauts' own: the models, the agents, the
tool servers and the workers. ``ONEBERTH_CONFIG`` names oneberth's: the sign-in through
identity providers, the allow list, the grants and the backend's public URL, under
``[apps.robinauts]``. ``--local`` is oneberth's local mode in its place: one person, the one at
the machine, signed in with a ticket, on a loopback address, and no oneberth file. Its launch
secret is the first line of standard input; a start by hand has none there, makes one, and
prints the address that signs the person in.

Storage is the PostgreSQL ``ROBINAUTS_DATABASE_URL`` names, with oneberth's store in its schema
``oneberth`` and neorc's in ``neorc``; or the data directory ``ROBINAUTS_DATA_DIR`` names, with
``robinauts.db``, ``oneberth.db`` and ``neorc.db`` in it; or this process, with neither, in the
local mode alone. The server never changes a PostgreSQL database's Robinauts or oneberth tables:
it refuses one that is not this build's and names the command. The flows' configuration is a
folder of its own, which ``ROBINAUTS_FLOWS_DIR`` names: neorc's worker then runs beside the
server (``orchestrator/``, ``docs/architecture/orchestration.md``).
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import logging
import os
import secrets
import shlex
import signal
import sys
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from importlib import resources
from pathlib import Path
from typing import Any

import httpx
from oneberth.admin import PostgresConnector, add_admin_commands
from oneberth.config import Config as OneberthConfig
from oneberth.config import ConfigError as OneberthConfigError
from oneberth.config import is_loopback, load_config, local_config
from oneberth.identity import (
    ONEBERTH_ROLES,
    OPERATOR,
    Access,
    CredentialKind,
    IdentityStore,
    NotFoundError,
    Registry,
    RoleTable,
    new_secret,
)
from oneberth.identity.memory import MemoryIdentityStore
from oneberth.stores import opened_store
from oneberth.web import (
    Component,
    Composition,
    Context,
    Listen,
    ServerError,
    compose_app,
    read_launch_secret,
    serve,
)

from robinauts.cli.composition import (
    CONTROLLER_TABLES,
    DATA_DIR_VARIABLE,
    DATABASE_URL_VARIABLE,
    ENGINE_REFUSALS,
    Composed,
    SecretLookup,
    compose,
    configure,
    init_database,
    load,
    read_tables,
    storage_from,
)
from robinauts.cli.lifecycle import Lifecycle, WorkerStartError
from robinauts.cli.orchestration import neorc_roles, orchestrator_of
from robinauts.controller.contract.domain import ConfigError
from robinauts.orchestrator.contract.domain import (
    MOVED,
    TABLE,
    OrchestrationStartError,
    Orchestrator,
)
from robinauts.shared_domain.domain import Config, ControllerError, StorageKind
from robinauts.web.app import NAME, new_robinauts_webapp
from robinauts.web.roles import ROBINAUTS_ROLES, WORKER_ROLE

REPO = Path(__file__).resolve().parents[4]

DEFAULT_PORT = 8000
WORKER_PORT_OFFSET = 1
"""The worker listener's port, when none is given: the front listener's, plus this."""

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
LOCAL_MODE = "oneberth's local mode: one person, signed in with a ticket, on a loopback address"
LOCAL_STORAGE = (
    "the records are in %s: robinauts.db, oneberth.db and neorc.db; each worker's engines keep"
    " their state in its folder under workers/ there"
)
STORE_INIT = "robinauts store init"

log = logging.getLogger(__name__)


def interface() -> Path:
    """``ROBINAUTS_UI_DIR`` when set; else the build a wheel carries as ``robinauts/ui``;
    else ``frontend/dist`` of the checkout this runs from."""
    named = os.environ.get("ROBINAUTS_UI_DIR")
    if named:
        return Path(named)
    packaged = Path(str(resources.files("robinauts"))) / "ui"
    if (packaged / "index.html").is_file():
        return packaged
    return REPO / "frontend" / "dist"


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
                f"--local serves this machine alone: --host {host} is not a loopback address"
                " or localhost"
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


class ServiceTokens:
    """The service tokens of this backend's own start: its workers', neorc's worker's, the
    flows' upload's and the handlers'. In the local mode the launch secret mints them, and they
    live in the backend's memory alone; else the operator does, under a name of this start's,
    and each is revoked at the stop. The launcher of a later step mints the workers' instead.
    """

    def __init__(self, composition: Composition) -> None:
        self._composition = composition
        self.access: Access | None = None
        self._made: list[Any] = []

    async def mint(self, name: str, role: str, resource: str | None) -> str:
        access = self.access
        assert access is not None, "the backend is not started"
        if self._composition.local and self._composition.launch_secret is not None:
            by: Any = await access.authenticate(self._composition.launch_secret, bearer=True)
        else:
            by = OPERATOR
            name = f"{name}-{secrets.token_hex(4)}"
        secret, token = await access.create_service_token(name, role, resource=resource, by=by)
        self._made.append(token.id)
        return secret

    async def revoke_all(self) -> None:
        access = self.access
        made, self._made = self._made, []
        if access is None:
            return
        for token in made:
            try:
                await access.revoke_service_token(token, by=OPERATOR)
            except NotFoundError:
                pass


class LateTransport(httpx.AsyncBaseTransport):
    """An httpx transport to an ASGI application named after it is built: how a part of the
    backend reaches the backend's own front listener, in its own process."""

    def __init__(self) -> None:
        self.app: Any = None

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return await httpx.ASGITransport(app=self.app).handle_async_request(request)


class Backend:
    """What ``robinauts start`` serves: the composition of Robinauts' component and neorc's,
    the lifecycle of the controller and the workers, and neorc's start."""

    def __init__(
        self,
        composed: Composed,
        lifecycle: Lifecycle,
        orchestrator: Orchestrator,
        *,
        oneberth: OneberthConfig,
        secret_for: SecretLookup,
        store: Callable[[], AbstractAsyncContextManager[Any]] | IdentityStore,
        ui_dir: Path | None = None,
        local: bool = False,
    ) -> None:
        self.composed = composed
        self.lifecycle = lifecycle
        self.orchestrator = orchestrator
        self.transport = LateTransport()
        self.component = new_robinauts_webapp(
            composed.controller,
            composed.brokers,
            worker_access=lifecycle.access,
            feed=lifecycle.feed,
            config=composed.config,
            secret_for=secret_for,
            lifespan=self._lifespan,
            health=self._healthy,
            flows=orchestrator.flows(self.transport),
        )
        neorc = orchestrator.component
        assert isinstance(neorc, Component)
        self.composition = compose_app(
            [self.component, neorc],
            config=oneberth,
            store=store,
            interface=ui_dir,
            local=local,
        )
        self.transport.app = self.composition.front
        self.tokens = ServiceTokens(self.composition)

    async def _healthy(self) -> bool:
        return self.lifecycle.healthy()

    @asynccontextmanager
    async def _lifespan(self, context: Context) -> AsyncIterator[None]:
        """Open neorc's storage, then the controller, then start the workers; and the
        reverse at the stop. neorc's component starts after this, and stops before."""
        self.tokens.access = context.access
        await self.orchestrator.open()
        try:
            await self.lifecycle.start(
                lambda name: self.tokens.mint(f"robinauts-worker-{name}", WORKER_ROLE, name)
            )
            try:
                yield
            finally:
                self.lifecycle.feed.close()
                try:
                    await self.orchestrator.stop()
                finally:
                    await self.lifecycle.stop()
        finally:
            try:
                await self.tokens.revoke_all()
            finally:
                await self.orchestrator.close()

    async def start_orchestrator(self) -> None:
        """Once the backend serves: upload the flows and start neorc's worker."""
        await self.orchestrator.start(self.tokens.mint, self.transport)

    async def stop_workers(self) -> None:
        """While the listeners still serve: a worker in a process of its own ends its turns
        through them. The change feed's polls end first."""
        self.lifecycle.feed.close()
        try:
            await self.orchestrator.stop()
        finally:
            await self.lifecycle.stop_worker()


async def serve_backend(
    backend: Backend,
    front: Listen,
    worker: Listen,
    *,
    launch_secret: str | None,
    by_hand: bool,
) -> int:
    """Serve until SIGINT or SIGTERM. ``ready`` is said once both listeners serve and neorc
    has started. A stop ends the workers while the listeners still serve, then the listeners,
    then the components in reverse."""
    composition = backend.composition
    up = asyncio.Event()
    stop = asyncio.Event()
    final = asyncio.Event()
    serving = asyncio.create_task(
        serve(
            composition,
            front,
            worker,
            ready=up.set,
            stop=final,
            launch_secret=launch_secret,
        )
    )
    started = asyncio.create_task(up.wait())
    await asyncio.wait({serving, started}, return_when=asyncio.FIRST_COMPLETED)
    if not up.is_set():
        started.cancel()
        await serving
        return 1
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    status = 0
    try:
        await backend.start_orchestrator()
    except OrchestrationStartError as refused:
        print(refused, file=sys.stderr)
        status = 1
    else:
        print("ready", flush=True)
        if by_hand and composition.launch_secret is not None:
            access = composition.access
            launcher = await access.authenticate(composition.launch_secret, bearer=True)
            ticket, _ = await access.create_ticket(launcher)
            print(f"open {composition.ticket_url(ticket)}", flush=True)
        stopping = asyncio.create_task(stop.wait())
        await asyncio.wait({serving, stopping}, return_when=asyncio.FIRST_COMPLETED)
        stopping.cancel()
    try:
        await backend.stop_workers()
    finally:
        final.set()
        await serving
    return status


def start(host: str, port: int, worker_host: str, worker_port: int | None, *, local: bool) -> int:
    ui_dir = interface()
    config_path = Path(os.environ["ROBINAUTS_CONFIG"])
    tables = read_tables(config_path)
    worker_port = port + WORKER_PORT_OFFSET if worker_port is None else worker_port
    try:
        config, secret_for, oneberth = serving(
            tables, os.environ, host=host, port=port, local=local
        )
        orchestrator = orchestrator_of(
            os.environ,
            config,
            default_code=REPO / "orchestration",
            public_url=oneberth.app(NAME).public_url,
            api_url=local_url(host, port),
            worker_url=local_url(worker_host, worker_port),
        )
    except ConfigError as refused:
        print(refused, file=sys.stderr)
        return 1
    storage = storage_from(os.environ)
    composed = compose(config, storage=storage, secret_for=secret_for)
    logging.basicConfig(level=logging.INFO)
    if local:
        log.warning(LOCAL_MODE)
    if storage.kind is StorageKind.LOCAL:
        log.warning(LOCAL_STORAGE, storage.path)
    for name, command in config.commands.items():
        log.info("worker %r starts with the command: %s", name, shlex.join(command))
    lifecycle = Lifecycle(
        composed,
        broker_url=f"{local_url(worker_host, worker_port)}/{NAME}",
        # A store in this process is this process's alone, and so is a SQLite file: its workers
        # run here, but for one that starts with a command of its own.
        in_process=storage.kind is not StorageKind.POSTGRES,
    )
    try:
        backend = Backend(
            composed,
            lifecycle,
            orchestrator,
            oneberth=oneberth,
            secret_for=secret_for,
            store=store_of(os.environ),
            ui_dir=ui_dir,
            local=local,
        )
        launch_secret, by_hand = None, False
        if local:
            launch_secret = read_launch_secret(sys.stdin)
            if launch_secret is None:
                launch_secret, by_hand = new_secret(CredentialKind.LAUNCH), True
        return asyncio.run(
            serve_backend(
                backend,
                Listen(host, port),
                Listen(worker_host, worker_port),
                launch_secret=launch_secret,
                by_hand=by_hand,
            )
        )
    except (
        ControllerError,
        OneberthConfigError,
        ServerError,
        *ENGINE_REFUSALS,
        WorkerStartError,
        OrchestrationStartError,
    ) as refused:
        # A database or an engine's file that will not open or is not this build's, an engine
        # the build lacks or a secret missing, a worker process or neorc that did not start:
        # said before anything listens.
        print(refused, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


def database_init() -> int:
    """Create this build's schema in the configured database, and set the engines up."""
    url = os.environ.get(DATABASE_URL_VARIABLE)
    if not url:
        print(f"{DATABASE_URL_VARIABLE} is not set", file=sys.stderr)
        return 2
    config, secret_for = load(Path(os.environ["ROBINAUTS_CONFIG"]), os.environ)
    try:
        print(asyncio.run(init_database(url, config, secret_for)))
    except ConfigError as refused:
        print(refused, file=sys.stderr)
        return 1
    return 0


def openapi_document() -> dict[str, Any]:
    """Robinauts' OpenAPI document, as the backend serves it at ``/robinauts/openapi.json``:
    what ``scripts/update-openapi.sh`` writes to ``backend/openapi.json``."""
    composed = compose(Config(), storage=storage_from({}), secret_for={}.get)
    url = local_url("127.0.0.1", DEFAULT_PORT)
    orchestrator = orchestrator_of(
        {},
        composed.config,
        default_code=REPO,
        public_url=url,
        api_url=url,
        worker_url=url,
    )
    backend = Backend(
        composed,
        Lifecycle(composed),
        orchestrator,
        oneberth=local_config(NAME, port=DEFAULT_PORT),
        secret_for={}.get,
        store=store_of({}),
        local=True,
    )

    async def read() -> dict[str, Any]:
        transport = httpx.ASGITransport(app=backend.composition.front)
        async with httpx.AsyncClient(transport=transport, base_url=url) as client:
            answer = await client.get(f"/{NAME}/openapi.json")
        answer.raise_for_status()
        document: dict[str, Any] = answer.json()
        return document

    return asyncio.run(read())


def registry() -> Registry:
    """The roles a deployment of Robinauts knows: oneberth's, Robinauts' and neorc's."""
    neorc = neorc_roles()
    assert isinstance(neorc, RoleTable)
    return Registry([ONEBERTH_ROLES, ROBINAUTS_ROLES, neorc])


def run(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="robinauts")
    commands = parser.add_subparsers(dest="command", required=True)
    serve_command = commands.add_parser("start", help="serve the backend")
    serve_command.add_argument("--host", default="127.0.0.1", help="the front listener's")
    serve_command.add_argument("--port", type=int, default=DEFAULT_PORT)
    serve_command.add_argument(
        "--worker-host",
        default="127.0.0.1",
        help="the worker listener's, where workers reach",
    )
    serve_command.add_argument(
        "--worker-port",
        type=int,
        default=None,
        help=f"the worker listener's port; the front's plus {WORKER_PORT_OFFSET} by default",
    )
    serve_command.add_argument(
        "--local",
        action="store_true",
        help="oneberth's local mode: the one person at this machine, signed in with a ticket",
    )
    database = commands.add_parser("db", help="Robinauts' tables in the deployment's database")
    database_commands = database.add_subparsers(dest="database_command", required=True)
    database_commands.add_parser("init", help="create this build's schema in an empty database")
    commands.add_parser("version")
    add_admin_commands(
        commands, PostgresConnector(registry(), database_url_env=DATABASE_URL_VARIABLE)
    )
    arguments = parser.parse_args(argv)
    if arguments.command == "start":
        status = start(
            arguments.host,
            arguments.port,
            arguments.worker_host,
            arguments.worker_port,
            local=arguments.local,
        )
    elif arguments.command == "db":
        status = database_init()
    elif arguments.command == "version":
        print(importlib.metadata.version("robinauts"))
        status = 0
    else:
        status = arguments.handler(arguments)
    if status:
        raise SystemExit(status)
