# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""``robinauts start`` serves the web shell, no sign-in; ``robinauts db init`` makes the
database ready; ``robinauts version`` prints the version.

The configuration is the TOML file ``ROBINAUTS_CONFIG`` names. The interface is
``frontend/dist`` of this checkout when it is built, or the directory ``ROBINAUTS_UI_DIR``
names. Storage is PostgreSQL when ``ROBINAUTS_DATABASE_URL`` is set, and in memory
otherwise. The server never changes the database: it refuses one that is not this build's
and names the command.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import logging
import os
import sys
from collections.abc import Sequence
from pathlib import Path

import uvicorn

from robinauts.controller.composition import (
    DATABASE_URL_VARIABLE,
    build,
    init_database,
    load,
    storage_from,
)
from robinauts.controller.contract.domain import ConfigError
from robinauts.web.app import create_app

REPO = Path(__file__).resolve().parents[4]

GRACEFUL_SHUTDOWN_SECONDS = 20
"""How long uvicorn waits for open streams on a stop before it closes them, so that the
controller's `close`, which runs after, still runs inside a service's stop timeout."""


def start(host: str, port: int) -> None:
    named = os.environ.get("ROBINAUTS_UI_DIR")
    ui_dir = Path(named) if named else REPO / "frontend" / "dist"
    config, secret_for = load(Path(os.environ["ROBINAUTS_CONFIG"]), os.environ)
    controller = build(config, storage=storage_from(os.environ), secret_for=secret_for)
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(
        create_app(controller, ui_dir=ui_dir),
        host=host,
        port=port,
        timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SECONDS,
    )


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
    database = commands.add_parser("db", help="the deployment's database")
    database_commands = database.add_subparsers(dest="database_command", required=True)
    database_commands.add_parser("init", help="create this build's schema in an empty database")
    commands.add_parser("version")
    arguments = parser.parse_args(argv)
    if arguments.command == "start":
        start(arguments.host, arguments.port)
    elif arguments.command == "db":
        status = database_init()
        if status:
            raise SystemExit(status)
    else:
        print(importlib.metadata.version("robinauts"))
