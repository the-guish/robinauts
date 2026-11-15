# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""A worker in a process of its own, one for each worker the configuration names: with this
module, or with the command the worker's table names, which runs this module elsewhere, say in a
container. Its supervisor starts it: oneberth's launcher on the desktop, or ``Lifecycle`` in a
server that starts its own workers.

It reads the configuration ``ROBINAUTS_CONFIG`` names, and runs only the worker its environment
names, until SIGTERM or SIGINT: the engines of that worker's agents, and the turns of those
agents, which are all the backend hands it. It then stops the worker, which waits for its tasks.
It reaches the core through the backend's API alone (``worker/http_turn_broker.py``), on the
worker listener, with the token it reads from its standard input, which says to that API which
worker this is. It writes a line on its standard output once the worker has started. On Linux it
also ends with its supervisor, killed or not, where its supervisor is its parent.

It reads the launcher's variables (oneberth's ``docs/specs/shapes.md``, "Starting a worker"),
and a server's where they are absent:

- the worker listener: ``ONEBERTH_WORKER_URL``, or the component's address there,
  ``ROBINAUTS_WORKER_BROKER_URL``;
- its name: ``ONEBERTH_WORKER_NAME``, or ``ROBINAUTS_WORKER_NAME``;
- its supervisor's process: ``ONEBERTH_LAUNCHER_PID``, or ``ROBINAUTS_WORKER_PARENT``;
- the data directory: ``ONEBERTH_DATA_DIR``, or ``ROBINAUTS_DATA_DIR``.

With a data directory, its engines keep their state in its own folder there,
``workers/<name>``, and it opens nothing else of the directory. A token in the file
``ONEBERTH_TOKEN_FILE`` names is read in the place of the standard input.
"""

from __future__ import annotations

import asyncio
import ctypes
import logging
import os
import signal
import sys
from collections.abc import Mapping
from pathlib import Path

from robinauts.cli.composition import (
    compose_worker,
    http_turn_broker,
    load,
    storage_from,
)
from robinauts.shared_domain.domain import DEFAULT_WORKER, StorageConfig, StorageKind
from robinauts.web.contract.broker.wire import COMPONENT, PREFIX

PARENT_VARIABLE = "ROBINAUTS_WORKER_PARENT"
"""The id of the process that spawned this one, which it ends with. Unset where a worker's
command runs this process away from the server, in a container or on another machine, whose
parent is not the server: there, the command's own end is the process's."""

READY_LINE = b"ready\n"
"""What this process writes on its standard output once its worker has started."""

BROKER_URL_VARIABLE = "ROBINAUTS_WORKER_BROKER_URL"
"""The root of the API this process reaches the core through: its parent's."""

WORKER_NAME_VARIABLE = "ROBINAUTS_WORKER_NAME"
"""The worker this process runs: ``default`` when unset."""

LAUNCHER_PID_VARIABLE = "ONEBERTH_LAUNCHER_PID"
"""The launcher's process id: its parent, which it ends with."""
WORKER_URL_VARIABLE = "ONEBERTH_WORKER_URL"
"""The launcher's: the address of the backend's worker listener."""
LAUNCHED_NAME_VARIABLE = "ONEBERTH_WORKER_NAME"
"""The launcher's: the worker's name."""
LAUNCHED_DATA_DIR_VARIABLE = "ONEBERTH_DATA_DIR"
"""The launcher's: the data directory, whose folder ``workers/<name>`` is this worker's."""
TOKEN_FILE_VARIABLE = "ONEBERTH_TOKEN_FILE"
"""A file that holds the token, as a platform gives it, in the place of the standard input."""

_PR_SET_PDEATHSIG = 1


def broker_url(environ: Mapping[str, str]) -> str:
    """The root of the API this process reaches the core through: Robinauts' component on the
    worker listener the launcher names, or the URL a server names."""
    listener = environ.get(WORKER_URL_VARIABLE)
    if listener:
        return listener.rstrip("/") + COMPONENT
    return environ[BROKER_URL_VARIABLE]


def worker_name(environ: Mapping[str, str]) -> str:
    """The worker this process runs: the launcher's name for it, a server's, or the default."""
    return (
        environ.get(LAUNCHED_NAME_VARIABLE) or environ.get(WORKER_NAME_VARIABLE) or DEFAULT_WORKER
    )


def worker_storage(environ: Mapping[str, str]) -> StorageConfig:
    """Where the engines keep their state: the launcher's data directory, whatever else the
    environment names, or the storage a server names."""
    folder = environ.get(LAUNCHED_DATA_DIR_VARIABLE)
    if folder:
        return StorageConfig(StorageKind.LOCAL, path=folder)
    return storage_from(environ)


class QuietRequests(logging.Filter):
    """Leaves out of httpx's log the worker's requests to the core, one per event a turn
    writes, which its server logs already; the engines' requests stay."""

    def __init__(self, url: str) -> None:
        super().__init__()
        self._root = url.rstrip("/") + PREFIX

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args if isinstance(record.args, tuple) else ()
        return not any(str(arg).startswith(self._root) for arg in args)


def end_with_parent(environ: Mapping[str, str]) -> None:
    """A SIGTERM when the parent ends, so that no part outlives its supervisor: the launcher,
    or the server that started it. Linux alone offers it; elsewhere a killed supervisor leaves
    its part running. A process whose parent is not its supervisor, run by a worker's command
    elsewhere, is left to that command."""
    parent = environ.get(LAUNCHER_PID_VARIABLE) or environ.get(PARENT_VARIABLE)
    if sys.platform != "linux" or not parent:
        return
    ctypes.CDLL(None, use_errno=True).prctl(_PR_SET_PDEATHSIG, signal.SIGTERM)
    # The parent may have ended before the request was made.
    if parent != str(os.getppid()):
        os.kill(os.getpid(), signal.SIGTERM)


def _read_secret() -> str:
    """The secret: from the file ``ONEBERTH_TOKEN_FILE`` names, or else the standard input, read
    to its end, which the supervisor closes once it has written it."""
    named = os.environ.get(TOKEN_FILE_VARIABLE)
    if named:
        return Path(named).read_text().strip()
    return sys.stdin.buffer.read().decode().strip()


async def _run() -> None:
    config, secret_for = load(Path(os.environ["ROBINAUTS_CONFIG"]), os.environ)
    url = broker_url(os.environ)
    logging.getLogger("httpx").addFilter(QuietRequests(url))
    name = worker_name(os.environ)
    broker = http_turn_broker(url, _read_secret())
    worker = compose_worker(
        config,
        storage=worker_storage(os.environ),
        secret_for=secret_for,
        broker=broker,
        name=name,
    )
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stopping.set)
    await worker.start()
    sys.stdout.buffer.write(READY_LINE)
    sys.stdout.buffer.flush()
    try:
        await stopping.wait()
    finally:
        await worker.stop()


def main() -> None:
    name = worker_name(os.environ)
    logging.basicConfig(
        level=logging.INFO, format=f"%(levelname)s:worker {name}:%(name)s:%(message)s"
    )
    end_with_parent(os.environ)
    asyncio.run(_run())


if __name__ == "__main__":
    main()
