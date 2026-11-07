# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""A worker in a process of its own, which ``Lifecycle`` spawns, one for each worker the
configuration names: with this module, or with the command the worker's table names, which
runs this module elsewhere, say in a container.

It reads the configuration and the environment ``robinauts start`` read, and runs only the
worker its environment names, until SIGTERM or SIGINT: the engines of that worker's agents,
and the turns of those agents, which are all its parent's API hands it. It then stops the
worker, which waits for its tasks. It reaches the core through its parent's API alone
(``worker/http_turn_broker.py``), at the URL its environment names, with the secret it reads
from its standard input, which says to that API which worker this is; the database it opens,
if its environment names one, is for the engines' state. It writes a line on its standard
output once the worker has started. On Linux it also ends with its parent, killed or not,
where its parent is the server.
"""

from __future__ import annotations

import asyncio
import ctypes
import logging
import os
import signal
import sys
from pathlib import Path

from robinauts.cli.composition import (
    compose_worker,
    http_turn_broker,
    load,
    storage_from,
)
from robinauts.shared_domain.domain import DEFAULT_WORKER
from robinauts.web.contract.broker.wire import PREFIX

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

_PR_SET_PDEATHSIG = 1


class QuietRequests(logging.Filter):
    """Leaves out of httpx's log the worker's requests to the core, one per event a turn
    writes, which its server logs already; the engines' requests stay."""

    def __init__(self, url: str) -> None:
        super().__init__()
        self._root = url.rstrip("/") + PREFIX

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args if isinstance(record.args, tuple) else ()
        return not any(str(arg).startswith(self._root) for arg in args)


def _end_with_parent() -> None:
    """A SIGTERM when the parent ends, so that no worker outlives its server. Linux alone
    offers it; elsewhere a killed server leaves its worker running. A process whose parent is
    not the server, run by a worker's command elsewhere, is left to that command."""
    if sys.platform != "linux" or PARENT_VARIABLE not in os.environ:
        return
    ctypes.CDLL(None, use_errno=True).prctl(_PR_SET_PDEATHSIG, signal.SIGTERM)
    # The parent may have ended before the request was made.
    if os.environ[PARENT_VARIABLE] != str(os.getppid()):
        os.kill(os.getpid(), signal.SIGTERM)


def _read_secret() -> str:
    """The secret, read from the standard input to its end, which the parent closed before this
    process began."""
    return sys.stdin.buffer.read().decode()


async def _run() -> None:
    config, secret_for = load(Path(os.environ["ROBINAUTS_CONFIG"]), os.environ)
    url = os.environ[BROKER_URL_VARIABLE]
    logging.getLogger("httpx").addFilter(QuietRequests(url))
    name = os.environ.get(WORKER_NAME_VARIABLE) or DEFAULT_WORKER
    broker = http_turn_broker(url, _read_secret())
    worker = compose_worker(
        config, storage=storage_from(os.environ), secret_for=secret_for, broker=broker, name=name
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
    name = os.environ.get(WORKER_NAME_VARIABLE) or DEFAULT_WORKER
    logging.basicConfig(
        level=logging.INFO, format=f"%(levelname)s:worker {name}:%(name)s:%(message)s"
    )
    _end_with_parent()
    asyncio.run(_run())


if __name__ == "__main__":
    main()
