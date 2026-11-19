# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The ``robinauts`` command: the one place that runs Robinauts, and that starts and stops its
parts in order. ``robinauts start`` serves the backend; ``robinauts db init`` makes Robinauts'
own tables ready on PostgreSQL; ``robinauts version`` prints the version. ``robinauts desktop``
starts the desktop with oneberth's launcher (``cli/desktop.py``), which runs the backend as
``robinauts desktop-backend`` and neorc's worker as ``robinauts neorc-worker``. ``store``,
``tokens``, ``sessions`` and ``devices`` are oneberth's admin scripts, with Robinauts' and
neorc's roles (oneberth's ``docs/specs/identity.md``, "Admin scripts").

The backend is a oneberth composition of two components: Robinauts' own, served under
``/robinauts/``, and neorc's manager, under ``/neorc/`` (``docs/architecture/web.md``). oneberth
serves ``/auth``, the interface under ``/ui/`` and ``/health``, on the front listener
(``--host``, ``--port``), and the workers' routes on the worker listener (``--worker-host``,
``--worker-port``). ``cli/backend.py`` builds the two components and wires them. This module
composes them alone, and a host with applications of its own composes them beside its own.

Two files configure it. ``ROBINAUTS_CONFIG`` names Robinauts' own: the models, the agents, the
tool servers and the workers. ``ONEBERTH_CONFIG`` names oneberth's: the sign-in through
identity providers, the allow list, the grants and the backend's public URL, under
``[apps.robinauts]``. ``--local`` is oneberth's local mode in its place: one person, the one at
the machine, signed in with a ticket, on a loopback address, and no oneberth file. Behind a
forward, such as a VM's port on its host's loopback, ``--public-host`` names that address and
``--host`` the one the front listener binds. Its launch secret is the first line of standard
input; a start by hand has none there, makes one, and prints the address that signs the person
in.

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
import shlex
import sys
from collections.abc import Mapping, Sequence
from importlib import resources
from pathlib import Path
from typing import Any

import httpx
from oneberth.admin import PostgresConnector, add_admin_commands
from oneberth.config import ConfigError as OneberthConfigError
from oneberth.config import local_config
from oneberth.identity import ONEBERTH_ROLES, Registry
from oneberth.launcher import LaunchedBackend, LauncherError, launch
from oneberth.web import Listen, compose_app
from oneberth.web import run as run_server

from robinauts.cli.backend import (
    COMMAND,
    DEFAULT_CODE,
    ONEBERTH_CONFIG_VARIABLE,
    REPO,
    Parts,
    local_url,
    robinauts_parts,
    store_of,
)
from robinauts.cli.composition import (
    DATA_DIR_VARIABLE,
    DATABASE_URL_VARIABLE,
    compose,
    init_database,
    load,
    storage_from,
)
from robinauts.cli.desktop import (
    CONFIG_FILE,
    CONFIG_TEMPLATE,
    IMAGE,
    IMAGE_VARIABLE,
    chosen_data_dir,
    copy_configuration,
    desktop_application,
)
from robinauts.cli.lifecycle import Lifecycle
from robinauts.cli.orchestration import (
    NEORC_ROLES,
    handlers_code,
    neorc_worker,
    orchestrator_of,
)
from robinauts.cli.worker_process import end_with_parent
from robinauts.controller.contract.domain import ConfigError
from robinauts.orchestrator.contract.domain import FLOWS_VARIABLE
from robinauts.shared_domain.domain import Config, StorageKind
from robinauts.web.app import NAME
from robinauts.web.roles import ROBINAUTS_ROLES

DEFAULT_PORT = 8000
WORKER_PORT_OFFSET = 1
"""The worker listener's port, when none is given: the front listener's, plus this."""

LOCAL_MODE = "oneberth's local mode: one person, signed in with a ticket, on a loopback address"
PUBLIC_HOST_IS_LOCAL = (
    f"--public-host is for --local: with sign-in, the public URL is in {ONEBERTH_CONFIG_VARIABLE}"
)
LOCAL_STORAGE = (
    "the records are in %s: robinauts.db, oneberth.db and neorc.db; each worker's engines keep"
    " their state in its folder under workers/ there"
)
NOT_LAUNCHED = (
    "desktop-backend is the desktop's backend, which the launcher starts: start the desktop"
    " with robinauts desktop"
)
STORE_INIT = "robinauts store init"
NOT_BUILT = (
    "the interface is not built in %s: /ui/ says so. Run npm run build in frontend/ and start again"
)

log = logging.getLogger(__name__)


def interface() -> Path | None:
    """``ROBINAUTS_UI_DIR`` when set; else the build a wheel carries as ``robinauts/ui``;
    else ``frontend/dist`` of the checkout this runs from. ``None`` when that directory holds
    no built interface: a checkout's, before ``npm run build``. The backend then serves its
    API, and ``/ui/`` answers that the interface is not built. A wheel always holds one."""
    named = os.environ.get("ROBINAUTS_UI_DIR")
    if named:
        chosen = Path(named)
    else:
        chosen = Path(str(resources.files("robinauts"))) / "ui"
        if not (chosen / "index.html").is_file():
            chosen = REPO / "frontend" / "dist"
    if (chosen / "index.html").is_file():
        return chosen
    log.warning(NOT_BUILT, chosen)
    return None


class Backend:
    """What ``robinauts start`` serves: Robinauts' parts composed alone, its own component and
    neorc's (``cli/backend.py``), with the built interface in ``ui_dir``."""

    def __init__(self, parts: Parts, *, ui_dir: Path | None = None) -> None:
        self.parts = parts
        self.composed = parts.composed
        self.lifecycle = parts.lifecycle
        self.orchestrator = parts.orchestrator
        self.composition = compose_app(
            list(parts.components),
            config=parts.oneberth,
            store=parts.store,
            interface=ui_dir,
            local=parts.local,
        )
        parts.composed_into(self.composition)


def start(
    host: str,
    port: int,
    worker_host: str,
    worker_port: int | None,
    *,
    local: bool,
    environ: Mapping[str, str] | None = None,
    public_host: str | None = None,
    supervised: bool = False,
) -> int:
    """Serve the backend until SIGINT or SIGTERM: its front listener on ``host`` and its worker
    listener on ``worker_host``, configured by ``environ``, this process's by default.
    ``public_host`` is where a browser reaches the front listener, ``host`` by default. A
    ``supervised`` backend starts no worker: its supervisor, the launcher, starts every one."""
    environ = os.environ if environ is None else environ
    front = Listen(host, port)
    worker = Listen(worker_host, port + WORKER_PORT_OFFSET if worker_port is None else worker_port)
    try:
        parts = robinauts_parts(
            environ,
            front=front,
            worker=worker,
            local=local,
            public_host=public_host,
            supervised=supervised,
        )
    except ConfigError as refused:
        print(refused, file=sys.stderr)
        return 1
    composed, config, storage = parts.composed, parts.composed.config, storage_from(environ)
    logging.basicConfig(level=logging.INFO)
    ui_dir = interface()
    if local:
        log.warning(LOCAL_MODE)
    if storage.kind is StorageKind.LOCAL:
        log.warning(LOCAL_STORAGE, storage.path)
    if supervised:
        log.info("the launcher starts the workers: %s", ", ".join(composed.workers))
    else:
        for name, command in config.commands.items():
            log.info("worker %r starts with the command: %s", name, shlex.join(command))
        for name in sorted(config.externals):
            log.info("worker %r is started elsewhere, with a token the operator created", name)
    try:
        backend = Backend(parts, ui_dir=ui_dir)
    except OneberthConfigError as refused:
        # A grant in oneberth's file names a role no component gives a person: said before
        # anything listens.
        print(refused, file=sys.stderr)
        return 1
    try:
        # Until SIGINT or SIGTERM. A backend that did not start exits 1, and oneberth's last
        # line says why: the component that failed, with what its error says. That is a
        # database or an engine's file that will not open or is not this build's, a worker
        # process that ended, or flows that neorc refused.
        run_server(backend.composition, front, worker)
    except KeyboardInterrupt:
        return 130
    return 0


def desktop_backend() -> int:
    """``robinauts desktop-backend``: the backend as oneberth's launcher starts it, on the
    desktop. The launcher names the data directory and where each listener is, and writes
    the launch secret on the standard input. The backend is in the local mode, keeps its
    records in the data directory, starts no worker, and ends with the launcher."""
    launched = LaunchedBackend.from_environ()
    if launched is None:
        print(NOT_LAUNCHED, file=sys.stderr)
        return 2
    end_with_parent(os.environ)
    environ = {**os.environ, DATA_DIR_VARIABLE: os.fspath(launched.data_dir)}
    environ.pop(DATABASE_URL_VARIABLE, None)
    environ.pop(ONEBERTH_CONFIG_VARIABLE, None)
    return start(
        launched.front_bind,
        launched.front_port,
        launched.worker_bind,
        launched.worker_port,
        local=True,
        environ=environ,
        public_host=launched.host,
        supervised=True,
    )


def desktop(data_dir: str | None, browser: str | None, arguments: Sequence[str]) -> int:
    """``robinauts desktop``: the backend, the workers and the window, started by oneberth's
    launcher on the data directory, until the window closes or a signal stops them. The
    configuration is the file ``ROBINAUTS_CONFIG`` names, else ``robinauts.toml`` in the data
    directory, which a first start makes empty. A worker in a container puts the desktop in the
    Docker shape, with the backend in a container of the image ``ROBINAUTS_IMAGE`` names."""
    folder = chosen_data_dir(data_dir, os.environ)
    folder.mkdir(parents=True, exist_ok=True)
    named = os.environ.get("ROBINAUTS_CONFIG")
    config_file = Path(named).absolute() if named else folder / CONFIG_FILE
    if not named and not config_file.exists():
        config_file.write_text(CONFIG_TEMPLATE)
        print(f"Robinauts' configuration is {config_file}: it names no agent yet", file=sys.stderr)
    flows_dir = os.environ.get(FLOWS_VARIABLE)
    flows = Path(flows_dir).absolute() if flows_dir else None
    try:
        config, _ = load(config_file, os.environ)
        application = desktop_application(
            config,
            data_dir=folder,
            config_file=config_file,
            flows=flows,
            code=None if flows is None else handlers_code(flows, default_code=DEFAULT_CODE),
            robinauts=COMMAND,
            browser=browser,
            arguments=arguments,
            image=os.environ.get(IMAGE_VARIABLE) or IMAGE,
            environ=os.environ,
        )
        copy_configuration(config, config_file, folder)
    except (ConfigError, LauncherError, OSError) as refused:
        print(refused, file=sys.stderr)
        return 1
    return launch(application)


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
        Parts(
            composed,
            Lifecycle(composed),
            orchestrator,
            oneberth=local_config(NAME, port=DEFAULT_PORT),
            secret_for={}.get,
            store=store_of({}),
            local=True,
        )
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
    return Registry([ONEBERTH_ROLES, ROBINAUTS_ROLES, NEORC_ROLES])


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
    serve_command.add_argument(
        "--public-host",
        default=None,
        help=(
            "with --local: the loopback address a browser reaches the front listener at, when"
            " a forward on that address reaches --host; --host by default"
        ),
    )
    desktop_command = commands.add_parser(
        "desktop", help="start the desktop: the backend, the workers and the window"
    )
    desktop_command.add_argument(
        "--data-dir",
        default=None,
        help="the data directory; ROBINAUTS_DATA_DIR, or this platform's by default",
    )
    desktop_command.add_argument(
        "--browser", default=None, help="the window's browser: Chrome or Chromium, found by default"
    )
    desktop_command.add_argument(
        "--browser-argument",
        action="append",
        default=[],
        help="an argument for the window's browser, after the launcher's own; repeatable",
    )
    commands.add_parser("desktop-backend", help="the desktop's backend, as the launcher starts it")
    commands.add_parser("neorc-worker", help="neorc's worker, as its supervisor starts it")
    database = commands.add_parser("db", help="Robinauts' tables in the deployment's database")
    database_commands = database.add_subparsers(dest="database_command", required=True)
    database_commands.add_parser("init", help="create this build's schema in an empty database")
    commands.add_parser("version")
    add_admin_commands(
        commands, PostgresConnector(registry(), database_url_env=DATABASE_URL_VARIABLE)
    )
    arguments = parser.parse_args(argv)
    if arguments.command == "start":
        if arguments.public_host is not None and not arguments.local:
            parser.error(PUBLIC_HOST_IS_LOCAL)
        status = start(
            arguments.host,
            arguments.port,
            arguments.worker_host,
            arguments.worker_port,
            local=arguments.local,
            public_host=arguments.public_host,
        )
    elif arguments.command == "desktop":
        status = desktop(arguments.data_dir, arguments.browser, arguments.browser_argument)
    elif arguments.command == "desktop-backend":
        status = desktop_backend()
    elif arguments.command == "neorc-worker":
        status = neorc_worker(os.environ, default_code=DEFAULT_CODE)
    elif arguments.command == "db":
        status = database_init()
    elif arguments.command == "version":
        print(importlib.metadata.version("robinauts"))
        status = 0
    else:
        status = arguments.handler(arguments)
    if status:
        raise SystemExit(status)
