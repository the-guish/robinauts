# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The desktop (oneberth's ``docs/specs/shapes.md``): what ``robinauts desktop`` describes to
oneberth's launcher, which starts it.

- The backend is ``robinauts desktop-backend``: oneberth's local mode, its records in the data
  directory, its listeners where the launcher says.
- Each worker the configuration names runs on its own, with a token of ``robinauts/worker``
  bound to its name: a process of ``robinauts.cli.worker_process`` or of its table's command, or
  a container of its table's image.
- With a flows folder, neorc's worker is one more process, ``robinauts neorc-worker``, a trusted
  worker with a token of ``neorc/worker`` bound to the handlers' queue.
- The window is Chrome or Chromium on the backend's front listener, signed in with a ticket.

One worker in a container puts the desktop in the Docker shape: the backend then runs in a
container of Robinauts' image, which sees the data directory at ``/data``. Each worker's
container sees its own folder of the data directory, ``workers/<name>``, at the same place, and
reads a copy of the configuration there. Every worker of this version is a trusted worker: a
container gets the keys of its agents, and joins the network with a way out.

The data directory holds ``robinauts.db``, ``neorc.db`` and ``oneberth.db``, a folder per worker
under ``workers/``, the window's profile and the launcher's own files. One launcher runs per
data directory: a second start brings the first one's window forward.
"""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path, PurePosixPath

from oneberth.launcher import (
    CONTAINER_DATA_DIR,
    WORKERS_DIRECTORY,
    Application,
    Backend,
    Container,
    Mount,
    Process,
    Run,
    Window,
    Worker,
    worker_directory,
)

from robinauts.cli.composition import DATA_DIR_VARIABLE, DATABASE_URL_VARIABLE
from robinauts.orchestrator.contract.domain import FLOWS_VARIABLE, HANDLER_QUEUE
from robinauts.shared_domain.domain import Config, ContainerConfig

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

IMAGE = "robinauts"
"""The image of the backend's container in the Docker shape, when ``ROBINAUTS_IMAGE`` names none:
the image the repository's ``Dockerfile`` builds, of the same build as this command."""
IMAGE_VARIABLE = "ROBINAUTS_IMAGE"

CONTAINER_CONFIG = "/etc/robinauts/robinauts.toml"
"""Where the backend's container sees a configuration that is outside the data directory."""

WORKER_CONFIG = "robinauts.toml"
"""The copy of the configuration in a container worker's folder, which the container reads."""

CONFIG_VARIABLE = "ROBINAUTS_CONFIG"


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


def secret_variables(config: Config) -> tuple[str, ...]:
    """The variables every key and secret of the configuration is read from."""
    named = {provider.api_key_env for provider in config.providers.values()}
    named |= {server.secret_env for server in config.tool_servers.values() if server.secret_env}
    return tuple(sorted(named))


def worker_secret_variables(config: Config, worker: str) -> tuple[str, ...]:
    """The variables the keys and secrets of that worker's agents are read from: those of the
    providers of their models, and those of their tool servers."""
    agents = config.for_worker(worker).agents.values()
    models = {model for agent in agents for model in agent.models if model in config.models}
    providers = {config.models[model].provider for model in models}
    named = {config.providers[p].api_key_env for p in providers if p in config.providers}
    tools = {tool for agent in agents for tool in agent.tools}
    named |= {
        server.secret_env
        for server in config.tool_servers.values()
        if server.id in tools and server.secret_env
    }
    return tuple(sorted(named))


def passed_on(names: Iterable[str], environ: Mapping[str, str]) -> dict[str, str]:
    """The variables of ``names`` that ``environ`` sets, with their values."""
    return {name: environ[name] for name in names if name in environ}


def container_folder(worker: str) -> PurePosixPath:
    """Where a worker's container sees its folder of the data directory."""
    return PurePosixPath(CONTAINER_DATA_DIR, WORKERS_DIRECTORY, worker)


def worker_container(
    config: Config, worker: str, container: ContainerConfig, environ: Mapping[str, str]
) -> Container:
    """The container of a worker: its folder of the data directory, the copy of the
    configuration in it, and the keys of its agents with the variables its table names, from
    ``environ``. Its folder is its home too: a container runs as the person's user, which the
    image does not know."""
    folder = container_folder(worker)
    names = (*worker_secret_variables(config, worker), *container.environment)
    return Container(
        container.image,
        environment={
            CONFIG_VARIABLE: os.fspath(folder / WORKER_CONFIG),
            DATA_DIR_VARIABLE: CONTAINER_DATA_DIR,
            "HOME": os.fspath(folder),
            **passed_on(names, environ),
        },
        mounts=[Mount(f"{WORKERS_DIRECTORY}/{worker}", os.fspath(folder))],
    )


def backend_container(
    config: Config,
    *,
    image: str,
    data_dir: Path,
    config_file: Path,
    flows: Path | None,
    code: Path | None,
    environ: Mapping[str, str],
) -> Container:
    """The backend's container in the Docker shape: it sees the data directory at ``/data``,
    the configuration, and the flows folder and the handlers' code at the paths the host has
    them, read-only. It gets every key and secret of the configuration from ``environ``."""
    mounts = []
    if config_file.is_relative_to(data_dir):
        inside = config_file.relative_to(data_dir)
        config_path = os.fspath(PurePosixPath(CONTAINER_DATA_DIR, inside))
    else:
        mounts.append(Mount(config_file, CONTAINER_CONFIG, read_only=True))
        config_path = CONTAINER_CONFIG
    if flows is not None:
        mounts.append(Mount(flows, os.fspath(flows), read_only=True))
        if code is not None and not code.is_relative_to(flows):
            mounts.append(Mount(code, os.fspath(code), read_only=True))
    return Container(
        image,
        ("robinauts", "desktop-backend"),
        {
            CONFIG_VARIABLE: config_path,
            DATABASE_URL_VARIABLE: "",
            "ONEBERTH_CONFIG": "",
            FLOWS_VARIABLE: "" if flows is None else os.fspath(flows),
            **passed_on(secret_variables(config), environ),
        },
        mounts,
    )


def copy_configuration(config: Config, config_file: Path, data_dir: Path) -> list[Path]:
    """Copy the configuration into the folder of each worker that runs in a container, where
    its container reads it. Returns the copies."""
    copies = []
    for worker in config.workers():
        if worker in config.containers:
            copy = worker_directory(data_dir, worker) / WORKER_CONFIG
            copy.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(config_file, copy)
            copies.append(copy)
    return copies


def desktop_application(
    config: Config,
    *,
    data_dir: Path,
    config_file: Path,
    flows: Path | None = None,
    code: Path | None = None,
    robinauts: Sequence[str] = (sys.executable, "-m", "robinauts.cli"),
    python: str = sys.executable,
    browser: str | None = None,
    arguments: Sequence[str] = (),
    image: str = IMAGE,
    environ: Mapping[str, str] | None = None,
) -> Application:
    """The desktop of ``config``, the configuration in ``config_file``, on ``data_dir``.
    ``robinauts`` is the command of this build, and ``python`` the interpreter of its worker
    module. ``flows`` is the flows folder, which adds neorc's worker, and ``code`` the folder of
    its handlers. ``browser`` is the window's browser, which the launcher looks for when none is
    named, and ``arguments`` go to it after the launcher's own. ``image`` is the backend's image
    in the Docker shape, and ``environ`` where the containers' keys are read, this process's
    environment by default."""
    environ = os.environ if environ is None else environ
    # Every process reads the one configuration, and none a database or a sign-in file: the
    # records are in the data directory, and the one person signs in with a ticket.
    shared = {
        CONFIG_VARIABLE: os.fspath(config_file),
        DATABASE_URL_VARIABLE: "",
        "ONEBERTH_CONFIG": "",
        FLOWS_VARIABLE: "" if flows is None else os.fspath(flows),
    }
    workers = []
    for name in config.workers():
        if name in config.externals:
            # Started elsewhere: the backend lets it in by its own token, as every worker.
            continue
        container = config.containers.get(name)
        run: Run = (
            worker_container(config, name, container, environ)
            if container is not None
            else Process(config.commands.get(name) or (python, "-m", WORKER_MODULE), shared)
        )
        workers.append(Worker(name, WORKER_ROLE, run, resource=name))
    if flows is not None:
        program = Process((*robinauts, "neorc-worker"), shared)
        workers.append(Worker(NEORC_WORKER, NEORC_WORKER_ROLE, program, resource=HANDLER_QUEUE))
    in_containers = bool(set(config.workers()) & set(config.containers))
    return Application(
        APPLICATION,
        data_dir,
        backend=Backend(
            Process((*robinauts, "desktop-backend"), shared),
            container=(
                backend_container(
                    config,
                    image=image,
                    data_dir=data_dir,
                    config_file=config_file,
                    flows=flows,
                    code=code,
                    environ=environ,
                )
                if in_containers
                else None
            ),
        ),
        workers=workers,
        front_port=FRONT_PORT,
        window=Window(browser, arguments),
    )
