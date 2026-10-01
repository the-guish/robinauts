# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""``robinauts start`` serves the web shell, no sign-in; ``robinauts version`` prints the version.

The configuration is the TOML file ``ROBINAUTS_CONFIG`` names. The interface is
``frontend/dist`` of this checkout when it is built, or the directory ``ROBINAUTS_UI_DIR``
names. Storage is in memory.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import logging
import os
from collections.abc import Sequence
from pathlib import Path

import uvicorn

from robinauts.controller.composition import build, load
from robinauts.controller.contract.domain import StorageConfig, StorageKind
from robinauts.web.app import create_app

REPO = Path(__file__).resolve().parents[4]


def start(host: str, port: int) -> None:
    named = os.environ.get("ROBINAUTS_UI_DIR")
    ui_dir = Path(named) if named else REPO / "frontend" / "dist"
    config, secret_for = load(Path(os.environ["ROBINAUTS_CONFIG"]), os.environ)
    controller = build(config, storage=StorageConfig(StorageKind.IN_MEMORY), secret_for=secret_for)
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(create_app(controller, ui_dir=ui_dir), host=host, port=port)


def run(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="robinauts")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("start")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    commands.add_parser("version")
    arguments = parser.parse_args(argv)
    if arguments.command == "start":
        start(arguments.host, arguments.port)
    else:
        print(importlib.metadata.version("robinauts"))
