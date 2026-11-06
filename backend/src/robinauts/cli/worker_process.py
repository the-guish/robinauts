# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The worker in a process of its own, which ``Lifecycle`` spawns on PostgreSQL.

It reads the configuration and the environment ``robinauts start`` read, and runs only the
worker, until SIGTERM or SIGINT. It then stops the worker, which waits for its tasks. It
reaches the core through its parent's API alone (``worker/http_turn_broker.py``), at the URL its
environment names, with the secret its parent wrote to a pipe; the database it opens is for
the engines' state. It writes a line to its parent once the worker has started. On Linux it
also ends with its parent, killed or not.
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
from robinauts.web.contract.broker.wire import PREFIX

PARENT_VARIABLE = "ROBINAUTS_WORKER_PARENT"
"""The id of the process that spawned this one, which it ends with."""

READY_VARIABLE = "ROBINAUTS_WORKER_READY_FD"
"""The pipe this process writes ``READY_LINE`` to once its worker has started."""

READY_LINE = b"ready\n"

ACTIVITY_VARIABLE = "ROBINAUTS_WORKER_ACTIVITY_FILE"
"""The file this process's worker touches at every heartbeat, which its parent's health check
reads."""

BROKER_URL_VARIABLE = "ROBINAUTS_WORKER_BROKER_URL"
"""The root of the API this process reaches the core through: its parent's."""

SECRET_VARIABLE = "ROBINAUTS_WORKER_SECRET_FD"
"""The pipe this process reads the secret it presents to that API from, to its end."""

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
    offers it; elsewhere a killed server leaves its worker running."""
    if sys.platform != "linux":
        return
    ctypes.CDLL(None, use_errno=True).prctl(_PR_SET_PDEATHSIG, signal.SIGTERM)
    # The parent may have ended before the request was made.
    if os.environ.get(PARENT_VARIABLE) != str(os.getppid()):
        os.kill(os.getpid(), signal.SIGTERM)


def _read_secret() -> str:
    """The secret, read to the pipe's end, which the parent closed before this one began."""
    pipe = int(os.environ[SECRET_VARIABLE])
    read = b""
    try:
        while chunk := os.read(pipe, 4096):
            read += chunk
    finally:
        os.close(pipe)
    return read.decode()


async def _run() -> None:
    config, secret_for = load(Path(os.environ["ROBINAUTS_CONFIG"]), os.environ)
    url = os.environ[BROKER_URL_VARIABLE]
    logging.getLogger("httpx").addFilter(QuietRequests(url))
    broker = http_turn_broker(url, _read_secret())
    worker = compose_worker(
        config, storage=storage_from(os.environ), secret_for=secret_for, broker=broker
    )
    worker.activity_file = Path(os.environ[ACTIVITY_VARIABLE])
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stopping.set)
    await worker.start()
    ready = int(os.environ[READY_VARIABLE])
    os.write(ready, READY_LINE)
    os.close(ready)
    try:
        await stopping.wait()
    finally:
        await worker.stop()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    _end_with_parent()
    asyncio.run(_run())


if __name__ == "__main__":
    main()
