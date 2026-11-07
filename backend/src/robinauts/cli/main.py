# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The ``robinauts`` command: the one place that runs Robinauts, and that starts and stops its
parts in order. ``robinauts start`` serves the web shell; ``robinauts db init`` makes the
database ready; ``robinauts version`` prints the version;
``robinauts devices list`` and ``robinauts devices end`` are the operator's hold on linked
devices (``docs/architecture/device-pairing.md``), over the database the server reads.

The configuration is the TOML file ``ROBINAUTS_CONFIG`` names. It holds the controller's
tables and sign-in's, read once and each half parsed by its own; a key that is neither is
refused. ``start`` needs a sign-in configuration that names an identity provider, or
``--dev-no-sign-in``, the local development mode, with no sign-in table and a loopback host
(``docs/specs/sign-in.md``). A start that is refused prints why and binds nothing. With a
provider, every ``/api/`` route answers for the person signed in through it; in the local
development mode, for the mode's one local user. The interface is the directory
``ROBINAUTS_UI_DIR`` names, or the build an installed wheel carries, or ``frontend/dist`` of
this checkout when it is built. Storage is the PostgreSQL ``ROBINAUTS_DATABASE_URL`` names,
which a start with sign-in needs; the local development mode is in this process without it. The
sign-in records are kept on the same storage. The server never changes the database: it
refuses one that is not this build's and names the command.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import logging
import os
import sys
import uuid
from collections.abc import Mapping, Sequence
from importlib import resources
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI

from robinauts.cli.composition import (
    CONTROLLER_TABLES,
    DATABASE_URL_VARIABLE,
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
from robinauts.controller.contract.domain import ConfigError, DeviceSession, User
from robinauts.controller.contract.ports import Credentials
from robinauts.shared_domain.domain import Config, ControllerError, StorageConfig, StorageKind
from robinauts.web.app import create_app
from robinauts.web.contract.broker.wire import SERVER_KEEP_ALIVE_SECONDS
from robinauts.web.sign_in import SIGN_IN_KEYS, SignInConfig, is_loopback, parse_sign_in

REPO = Path(__file__).resolve().parents[4]

GRACEFUL_SHUTDOWN_SECONDS = 20
"""How long uvicorn waits for open streams on a stop before it closes them, so that the
controller's `close`, which runs after, still runs inside a service's stop timeout."""

NO_PROVIDER = (
    "no identity provider is configured: name one in [providers], or start with"
    " --dev-no-sign-in to develop on this machine with sign-in off"
)
NO_DATABASE = f"no database: set {DATABASE_URL_VARIABLE} to the PostgreSQL this deployment uses"
SIGN_IN_OFF = "sign-in is off: the local development mode, one local user, loopback only"


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
    tables: Mapping[str, Any], environ: Mapping[str, str], *, host: str, dev_no_sign_in: bool
) -> tuple[Config, SecretLookup, SignInConfig | None]:
    """What ``start`` serves with: the controller's configuration, how a secret is read, and
    sign-in's, ``None`` in the local development mode. ``ConfigError`` names every problem of
    a start that is refused."""
    problems = [
        f"{key}: unknown key, neither a table of the controller's nor of sign-in's"
        for key in tables
        if key not in CONTROLLER_TABLES | SIGN_IN_KEYS
    ]
    sign_in = None
    if dev_no_sign_in:
        if held := sorted(SIGN_IN_KEYS & tables.keys()):
            problems.append(
                "--dev-no-sign-in cannot be combined with a sign-in configuration, and the file"
                f" holds {', '.join(held)}"
            )
        if not is_loopback(host):
            problems.append(
                f"--dev-no-sign-in serves this machine alone: --host {host} is not a loopback"
                " address or localhost"
            )
    else:
        try:
            sign_in = parse_sign_in(tables)
        except ConfigError as refused:
            problems.append(str(refused))
        else:
            if sign_in is None or not sign_in.providers:
                problems.append(NO_PROVIDER)
        if not environ.get(DATABASE_URL_VARIABLE):
            problems.append(NO_DATABASE)
    try:
        config, secret_for = configure(tables, environ)
    except ConfigError as refused:
        problems.append(str(refused))
    if problems:
        raise ConfigError("\n".join(problems))
    return config, secret_for, sign_in


def worker_broker_url(host: str, port: int) -> str:
    """Where a worker on this machine reaches the server that listens on ``host``: a loopback
    address for a server that listens on every address, else that host."""
    if host in ("", "0.0.0.0"):
        host = "127.0.0.1"
    elif host == "::":
        host = "::1"
    if ":" in host:
        host = f"[{host}]"
    return f"http://{host}:{port}"


def build_app(
    composed: Composed,
    lifecycle: Lifecycle,
    *,
    sign_in: SignInConfig | None,
    secret_for: SecretLookup,
    ui_dir: Path | None = None,
) -> FastAPI:
    """The HTTP server over what the composition built, with the lifecycle's gate for a worker
    in a process of its own and its health. The app starts and stops nothing: ``serve`` does."""
    return create_app(
        composed.controller,
        composed.credentials,
        composed.broker,
        worker_access=lifecycle.access,
        healthy=lifecycle.healthy,
        sign_in=sign_in,
        secret_for=secret_for,
        ui_dir=ui_dir,
    )


async def serve(server: Server, lifecycle: Lifecycle) -> None:
    """Serve until asked to stop: ``Server`` starts the controller and the worker as it starts
    up and stops them as it shuts down. The stop here is for a server that never got as far
    as its shut-down, and does nothing after the server's."""
    try:
        await server.serve()
    finally:
        await lifecycle.stop()


class Server(uvicorn.Server):
    """uvicorn's server, which opens the controller and starts the worker as it starts up,
    stops the worker before it stops listening, and closes the controller once it has. A
    worker in a process of its own ends the turns it runs through this server's API, which
    the app's shut-down, run once nothing listens any more, would be too late for.

    Both run inside ``serve``, where uvicorn holds the signals it handles: a signal during the
    start waits for it to end and then shuts the server down, and one that stops the server is
    raised again only once ``serve`` returns, which on SIGTERM ends the process at once."""

    def __init__(self, config: uvicorn.Config, lifecycle: Lifecycle) -> None:
        super().__init__(config)
        self._lifecycle = lifecycle

    async def startup(self, sockets: list[Any] | None = None) -> None:
        await self._lifecycle.start()
        # Even when asked to stop meanwhile: uvicorn shuts down, and so stops the lifecycle,
        # only a server that started.
        await super().startup(sockets=sockets)

    async def shutdown(self, sockets: list[Any] | None = None) -> None:
        try:
            await self._lifecycle.stop_worker()
        finally:
            try:
                await super().shutdown(sockets=sockets)
            finally:
                await self._lifecycle.stop()


def start(host: str, port: int, *, dev_no_sign_in: bool) -> int:
    ui_dir = interface()
    tables = read_tables(Path(os.environ["ROBINAUTS_CONFIG"]))
    try:
        config, secret_for, sign_in = serving(
            tables, os.environ, host=host, dev_no_sign_in=dev_no_sign_in
        )
    except ConfigError as refused:
        print(refused, file=sys.stderr)
        return 1
    storage = storage_from(os.environ)
    composed = compose(config, storage=storage, secret_for=secret_for)
    logging.basicConfig(level=logging.INFO)
    if dev_no_sign_in:
        logging.getLogger(__name__).warning(SIGN_IN_OFF)
    lifecycle = Lifecycle(
        composed,
        # A store in this process is this process's alone: its worker runs here.
        broker_url=(
            worker_broker_url(host, port) if storage.kind is StorageKind.POSTGRES else None
        ),
    )
    app = build_app(composed, lifecycle, sign_in=sign_in, secret_for=secret_for, ui_dir=ui_dir)
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SECONDS,
        timeout_keep_alive=SERVER_KEEP_ALIVE_SECONDS,
    )
    server = Server(config, lifecycle)
    try:
        asyncio.run(serve(server, lifecycle), loop_factory=config.get_loop_factory())
    except (ControllerError, WorkerStartError) as refused:
        # A database that will not open or is not this build's, an engine the build lacks or a
        # secret missing, a worker process that did not start: said before anything listens.
        print(refused, file=sys.stderr)
        return 1
    return 0


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


def device_line(user: User, device: DeviceSession) -> str:
    """One device, as the operator reads it: whose, what, and its three dates."""
    who = user.email or f"{user.provider}:{user.subject}"
    return "\t".join(
        (
            str(device.id),
            who,
            device.device.name,
            device.device.platform,
            f"linked {device.created_at.isoformat(timespec='seconds')}",
            f"last used {device.last_used_at.isoformat(timespec='seconds')}",
            f"expires {device.expires_at.isoformat(timespec='seconds')}",
        )
    )


async def devices_command(credentials: Credentials, arguments: argparse.Namespace) -> int:
    """``devices list`` or ``devices end``, over these credentials. ``end --user`` names a
    person by email, and is refused when that email is several users'."""
    if arguments.devices_command == "list":
        for user, device in await credentials.all_device_sessions():
            print(device_line(user, device))
        return 0
    owners = {u.id: u for u, _ in await credentials.all_device_sessions()}
    if arguments.user is not None:
        users = await credentials.users_with_email(arguments.user)
        if not users:
            print(f"no user has the email {arguments.user}", file=sys.stderr)
            return 1
        if len(users) > 1:
            print(
                f"{len(users)} users have the email {arguments.user}, one per provider:"
                " end their devices one by one with --device",
                file=sys.stderr,
            )
            return 1
        ended = await credentials.delete_device_sessions(user_id=users[0].id)
    elif arguments.device is not None:
        ended = await credentials.delete_device_sessions(device_id=arguments.device)
        if not ended:
            print(f"no device has the id {arguments.device}", file=sys.stderr)
            return 1
    else:
        ended = await credentials.delete_device_sessions(every=True)
    for device in ended:
        owner = owners.get(device.user_id) or User(device.user_id, "?", str(device.user_id))
        print("ended\t" + device_line(owner, device))
    print(f"{len(ended)} device(s) ended")
    return 0


def devices(arguments: argparse.Namespace) -> int:
    """The devices commands, on the configured database: no browser and no admin needed."""
    url = os.environ.get(DATABASE_URL_VARIABLE)
    if not url:
        print(f"{DATABASE_URL_VARIABLE} is not set", file=sys.stderr)
        return 2
    composed = compose(
        Config(), storage=StorageConfig(StorageKind.POSTGRES, url=url), secret_for={}.get
    )

    async def go() -> int:
        await composed.controller.open()
        try:
            return await devices_command(composed.credentials, arguments)
        finally:
            await composed.controller.close()

    try:
        return asyncio.run(go())
    except ConfigError as refused:
        print(refused, file=sys.stderr)
        return 1


def run(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="robinauts")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("start")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument(
        "--dev-no-sign-in",
        action="store_true",
        help="the local development mode: no sign-in, one local user, loopback only",
    )
    database = commands.add_parser("db", help="the deployment's database")
    database_commands = database.add_subparsers(dest="database_command", required=True)
    database_commands.add_parser("init", help="create this build's schema in an empty database")
    commands.add_parser("version")
    linked = commands.add_parser("devices", help="the linked devices, for the operator")
    linked_commands = linked.add_subparsers(dest="devices_command", required=True)
    linked_commands.add_parser("list", help="every linked device, with its user")
    end = linked_commands.add_parser("end", help="end linked devices: they get 401 at once")
    which = end.add_mutually_exclusive_group(required=True)
    which.add_argument("--user", metavar="EMAIL", help="every device of the user of that email")
    which.add_argument("--device", metavar="ID", type=uuid.UUID, help="one device, by its id")
    which.add_argument("--all", action="store_true", help="every device of the deployment")
    arguments = parser.parse_args(argv)
    if arguments.command == "start":
        status = start(arguments.host, arguments.port, dev_no_sign_in=arguments.dev_no_sign_in)
    elif arguments.command == "db":
        status = database_init()
    elif arguments.command == "devices":
        status = devices(arguments)
    else:
        print(importlib.metadata.version("robinauts"))
        status = 0
    if status:
        raise SystemExit(status)
