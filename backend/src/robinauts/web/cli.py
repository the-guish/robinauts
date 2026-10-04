# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""``robinauts start`` serves the web shell; ``robinauts db init`` makes the database ready;
``robinauts version`` prints the version.

The configuration is the TOML file ``ROBINAUTS_CONFIG`` names. It holds the controller's
tables and sign-in's, read once and each half parsed by its own; a key that is neither is
refused. ``start`` needs a sign-in configuration that names an identity provider, or
``--dev-no-sign-in``, the local development mode, with no sign-in table and a loopback host
(``docs/specs/sign-in.md``). A start that is refused prints why and binds nothing. With a
provider, every ``/api/`` route answers for the person signed in through it; in the local
development mode, for the mode's one local user. The interface is the directory
``ROBINAUTS_UI_DIR`` names, or the build an installed wheel carries, or ``frontend/dist`` of
this checkout when it is built. Storage is PostgreSQL, which ``ROBINAUTS_DATABASE_URL`` names
and a start with sign-in refuses to go without: one replica that silently kept its records in
memory would be a deployment of its own. The local development mode keeps them in memory when
the variable is unset. The sign-in records are kept on the same storage. The server never
changes the database: it refuses one that is not this build's and names the command.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import logging
import os
import socket
import sys
from collections.abc import Callable, Mapping, Sequence
from importlib import resources
from pathlib import Path
from types import FrameType
from typing import Any

import uvicorn

from robinauts.controller.composition import (
    CONTROLLER_TABLES,
    DATABASE_URL_VARIABLE,
    SecretLookup,
    compose,
    configure,
    init_database,
    load,
    read_tables,
    storage_from,
)
from robinauts.controller.contract.domain import Config, ConfigError
from robinauts.web.app import create_app
from robinauts.web.sign_in import SIGN_IN_KEYS, SignInConfig, is_loopback, parse_sign_in

REPO = Path(__file__).resolve().parents[4]

GRACEFUL_SHUTDOWN_SECONDS = 10
"""How long uvicorn waits for open requests on a stop before it closes them. The streams end
at once, when the drain starts; the controller's `close`, which runs after, gives the turns
running here `[work] drain_seconds` to finish, and its last waits ten seconds more."""


class DrainingServer(uvicorn.Server):
    """uvicorn's server, whose stop signal (`SIGTERM`, `SIGINT`) starts the app's drain first:
    `/ready` answers 503, no turn is taken, and every stream ends with a hint to re-attach,
    so that uvicorn has no long connection left to wait for."""

    def __init__(self, config: uvicorn.Config, drain: Callable[[], None]) -> None:
        super().__init__(config)
        self._drain = drain
        self._loop: asyncio.AbstractEventLoop | None = None

    async def startup(self, sockets: list[socket.socket] | None = None) -> None:
        self._loop = asyncio.get_running_loop()
        await super().startup(sockets)

    def handle_exit(self, sig: int, frame: FrameType | None) -> None:
        # A signal handler: the drain is handed to the loop rather than run in the middle of
        # whatever the loop was doing.
        if self._loop is not None and not self.should_exit:
            self._loop.call_soon_threadsafe(self._drain)
        super().handle_exit(sig, frame)


def serve(app: Any, host: str, port: int) -> None:
    """Serve the app until a stop signal, draining it first."""
    config = uvicorn.Config(
        app, host=host, port=port, timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SECONDS
    )
    DrainingServer(config, app.state.drain).run()


NO_PROVIDER = (
    "no identity provider is configured: name one in [providers], or start with"
    " --dev-no-sign-in to develop on this machine with sign-in off"
)
NO_DATABASE = (
    f"no database: set {DATABASE_URL_VARIABLE} to the PostgreSQL this deployment uses; only"
    " --dev-no-sign-in keeps its records in memory"
)
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
    composed = compose(config, storage=storage_from(os.environ), secret_for=secret_for)
    logging.basicConfig(level=logging.INFO)
    if dev_no_sign_in:
        logging.getLogger(__name__).warning(SIGN_IN_OFF)
    app = create_app(
        composed.controller,
        credentials=composed.credentials,
        sign_in=sign_in,
        secret_for=secret_for,
        ui_dir=ui_dir,
        operations=composed.operations,
    )
    serve(app, host, port)
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
    arguments = parser.parse_args(argv)
    if arguments.command == "start":
        status = start(arguments.host, arguments.port, dev_no_sign_in=arguments.dev_no_sign_in)
    elif arguments.command == "db":
        status = database_init()
    else:
        print(importlib.metadata.version("robinauts"))
        status = 0
    if status:
        raise SystemExit(status)
