# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The desktop, as processes (oneberth's ``docs/specs/shapes.md``): what ``robinauts desktop``
describes to oneberth's launcher, which starts it.

- The backend is one process, ``robinauts desktop-backend``: oneberth's local mode, its records
  in the data directory, its listeners where the launcher says.
- Each worker the configuration names is a process of its own, ``robinauts.cli.worker_process``
  or its table's command, with a token of ``robinauts/worker`` bound to its name.
- With a flows folder, neorc's worker is one more, ``robinauts neorc-worker``, a trusted worker
  with a token of ``neorc/worker`` bound to the handlers' queue.
- The window is Chrome or Chromium on the backend's front listener, signed in with a ticket.

The data directory holds ``robinauts.db``, ``neorc.db`` and ``oneberth.db``, a folder per worker
under ``workers/``, the window's profile and the launcher's own files. One launcher runs per
data directory: a second start brings the first one's window forward.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from oneberth.launcher import Application, Backend, Process, Window, Worker

from robinauts.cli.composition import DATABASE_URL_VARIABLE
from robinauts.orchestrator.contract.domain import FLOWS_VARIABLE, HANDLER_QUEUE
from robinauts.shared_domain.domain import Config

APPLICATION = "robinauts"

FRONT_PORT = 8420
"""The front listener's port at the first start in a data directory, when it is free. Every
later start keeps the port of the first: a browser keeps the theme and the open application
per origin."""

CONFIG_FILE = "robinauts.toml"
"""The configuration in the data directory, when ``ROBINAUTS_CONFIG`` names none."""

CONFIG_TEMPLATE = """\
# Robinauts' configuration: the model providers, the models, the agents, the tool servers and
# the workers. docs/deployment.md in Robinauts' repository describes each table.
"""

WORKER_ROLE = "robinauts/worker"
NEORC_WORKER = "neorc"
"""The launcher's name for neorc's worker."""
NEORC_WORKER_ROLE = "neorc/worker"

WORKER_MODULE = "robinauts.cli.worker_process"


def default_data_dir(platform: str, environ: Mapping[str, str], home: Path) -> Path:
    """Where a person's data lives when they name no other folder: under the application
    support folder on macOS, and under ``XDG_DATA_HOME`` elsewhere, which is
    ``~/.local/share`` when unset."""
    if platform == "darwin":
        return home / "Library" / "Application Support" / "Robinauts"
    xdg = environ.get("XDG_DATA_HOME", "")
    base = Path(xdg) if xdg and Path(xdg).is_absolute() else home / ".local" / "share"
    return base / "robinauts"


def chosen_data_dir(named: str | None, environ: Mapping[str, str]) -> Path:
    """The data directory: the one named on the command line, else ``ROBINAUTS_DATA_DIR``,
    else this platform's."""
    chosen = named or environ.get("ROBINAUTS_DATA_DIR")
    if chosen:
        return Path(chosen).expanduser().absolute()
    return default_data_dir(sys.platform, environ, Path.home())


def desktop_application(
    config: Config,
    *,
    data_dir: Path,
    config_file: Path,
    flows: Path | None = None,
    robinauts: Sequence[str] = (sys.executable, "-m", "robinauts.cli"),
    python: str = sys.executable,
    browser: str | None = None,
    arguments: Sequence[str] = (),
) -> Application:
    """The desktop of ``config``, the configuration in ``config_file``, on ``data_dir``.
    ``robinauts`` is the command of this build, and ``python`` the interpreter of its worker
    module. ``flows`` is the flows folder, which adds neorc's worker. ``browser`` is the
    window's browser, which the launcher looks for when none is named, and ``arguments`` go to
    it after the launcher's own."""
    # Every part reads the one configuration, and none a database or a sign-in file: the
    # records are in the data directory, and the one person signs in with a ticket.
    shared = {
        "ROBINAUTS_CONFIG": os.fspath(config_file),
        DATABASE_URL_VARIABLE: "",
        "ONEBERTH_CONFIG": "",
        FLOWS_VARIABLE: "" if flows is None else os.fspath(flows),
    }
    workers = [
        Worker(
            name,
            WORKER_ROLE,
            Process(config.commands.get(name) or (python, "-m", WORKER_MODULE), shared),
            resource=name,
        )
        for name in config.workers()
    ]
    if flows is not None:
        program = Process((*robinauts, "neorc-worker"), shared)
        workers.append(Worker(NEORC_WORKER, NEORC_WORKER_ROLE, program, resource=HANDLER_QUEUE))
    return Application(
        APPLICATION,
        data_dir,
        backend=Backend(Process((*robinauts, "desktop-backend"), shared)),
        workers=workers,
        front_port=FRONT_PORT,
        window=Window(browser, arguments),
    )
