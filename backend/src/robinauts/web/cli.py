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
this checkout when it is built. Storage is the PostgreSQL ``ROBINAUTS_DATABASE_URL`` names,
which a start with sign-in needs; the local development mode is in memory without it. The
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
from collections.abc import Mapping, Sequence
from importlib import resources
from pathlib import Path
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
from robinauts.controller.contract.domain import Config, ConfigError, StorageKind
from robinauts.web.app import create_app
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
    uvicorn.run(
        create_app(
            composed,
            sign_in=sign_in,
            secret_for=secret_for,
            ui_dir=ui_dir,
            # A store in memory is this process's alone: its worker runs here.
            spawn_worker_in_subprocess=storage.kind is StorageKind.POSTGRES,
        ),
        host=host,
        port=port,
        timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SECONDS,
    )
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
