# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The start and the stop of what the composition built, in order: what ``robinauts start``
runs around the HTTP server (``cli/main.py``), and what a test runs around an app or in its
place. The worker runs in this process,
or in a process of its own (``broker_url``), which reaches the core through this server's API
alone, with a secret handed to it at its start through a pipe, never its environment."""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import signal
import sys
import tempfile
import time
from pathlib import Path

from robinauts.cli.composition import Composed
from robinauts.cli.worker_process import (
    ACTIVITY_VARIABLE,
    BROKER_URL_VARIABLE,
    PARENT_VARIABLE,
    READY_LINE,
    READY_VARIABLE,
    SECRET_VARIABLE,
)
from robinauts.web.worker_api import WorkerAccess

_log = logging.getLogger(__name__)

WORKER_MODULE = "robinauts.cli.worker_process"


class WorkerStartError(RuntimeError):
    """The worker's process ended, or said nothing for ``WORKER_START_SECONDS``, before its
    worker started."""


WORKER_START_SECONDS = 60.0
"""How long `start` waits for the worker's process to say its worker has started, before it
kills it and fails."""

HEALTHY_WITHIN = 300.0
"""Seconds: a worker that has not touched its activity file for longer is unhealthy."""

WORKER_STOP_SECONDS = 15.0
"""How long `stop` waits for the worker's process after its SIGTERM before it kills it: more
than the worker waits for its tasks."""


class Lifecycle:
    def __init__(self, composed: Composed, *, broker_url: str | None = None) -> None:
        """``broker_url`` runs the worker in a process of its own, which reaches the core at that
        URL: this server's, as the worker's machine reaches it. That suits PostgreSQL alone,
        where the engines' state is in the database: an engine in this process would hold nothing of
        a session another process ran."""
        self.composed = composed
        self._broker_url = broker_url
        self._worker_running = False
        self._opened = False
        self._process: asyncio.subprocess.Process | None = None
        self._watching: asyncio.Task[None] | None = None
        self.access = WorkerAccess()
        """What lets the worker's process in through the API, while it runs."""
        self.activity_file = Path(tempfile.gettempdir()) / f"robinauts-worker-{os.getpid()}.alive"
        """What this node's worker touches at every heartbeat, in this process or its own."""

    async def start(self) -> None:
        """Open the controller, then start the worker. Return once the worker has started, so
        that a server which says it is up runs turns. A worker process is let in through the
        API from then on, with the secret it reads from a pipe at its start."""
        await self.composed.controller.open()
        self._opened = True
        if self._broker_url is None:
            self.composed.worker.activity_file = self.activity_file
            await self.composed.worker.start()
            self._worker_running = True
            return
        secret = secrets.token_urlsafe(32)
        self.access.open(secret)
        await self.composed.broker.listen_for_cancels(self.access.stop_requested)
        environment = {
            **os.environ,
            PARENT_VARIABLE: str(os.getpid()),
            ACTIVITY_VARIABLE: str(self.activity_file),
            BROKER_URL_VARIABLE: self._broker_url,
        }
        try:
            # The secret, whole, before the worker exists: far shorter than a pipe holds.
            secret_end, secret_write = os.pipe()
            try:
                os.write(secret_write, secret.encode())
            finally:
                os.close(secret_write)
            try:
                read_end, write_end = os.pipe()
            except BaseException:
                os.close(secret_end)
                raise
            environment[READY_VARIABLE] = str(write_end)
            environment[SECRET_VARIABLE] = str(secret_end)
            try:
                try:
                    self._process = await asyncio.create_subprocess_exec(
                        sys.executable,
                        "-m",
                        WORKER_MODULE,
                        env=environment,
                        pass_fds=(write_end, secret_end),
                    )
                finally:
                    os.close(write_end)
                    os.close(secret_end)
                await self._wait_until_ready(self._process, read_end)
            finally:
                os.close(read_end)
        except BaseException:
            self.access.close()
            raise
        self._worker_running = True
        self._watching = asyncio.create_task(self._watch(self._process), name="worker process")

    async def _wait_until_ready(self, process: asyncio.subprocess.Process, ready: int) -> None:
        """Wait for the worker's line on the pipe ``ready``, or stop its process and fail: it
        ended first, or it took longer than ``WORKER_START_SECONDS``."""
        loop = asyncio.get_running_loop()
        readable = loop.create_future()
        # Called again for as long as the pipe stays readable, until it is removed.
        loop.add_reader(ready, lambda: readable.done() or readable.set_result(None))
        try:
            await asyncio.wait_for(readable, WORKER_START_SECONDS)
            # The line is one write, shorter than a pipe's atomic size; at an end, b"".
            line = os.read(ready, len(READY_LINE))
        except TimeoutError:
            line = b""
        finally:
            loop.remove_reader(ready)
        if line != READY_LINE:
            await self._stop_process(process)
            raise WorkerStartError(
                f"the worker process did not start within {WORKER_START_SECONDS:g} s"
            )

    def healthy(self) -> bool:
        """Whether this node's worker has touched its activity file within ``HEALTHY_WITHIN``."""
        try:
            touched = self.activity_file.stat().st_mtime
        except FileNotFoundError:
            return False
        return time.time() - touched <= HEALTHY_WITHIN

    async def stop_worker(self) -> None:
        """Stop the worker, which waits for the turns it runs, unless it is stopped already. A
        server does it while it still listens: a worker in a process of its own ends its turns
        through the API. Then the API lets no worker in."""
        if self._worker_running:
            self._worker_running = False
            if self._process is None:
                await self.composed.worker.stop()
            else:
                await self._stop_process(self._process)
        self.access.close()

    async def stop(self) -> None:
        """Stop the worker, if it still runs, then close the controller, if it is open. Again,
        or after a start that failed, it does only what is left, and awaits nothing once all
        is done: a stop a signal has cancelled already must not be cut short by it."""
        await self.stop_worker()
        self.activity_file.unlink(missing_ok=True)
        if self._opened:
            self._opened = False
            await self.composed.controller.close()
            _log.info("the controller is closed")

    async def _stop_process(self, process: asyncio.subprocess.Process) -> None:
        if self._watching is not None:
            self._watching.cancel()
            await asyncio.gather(self._watching, return_exceptions=True)
        if process.returncode is None:
            process.send_signal(signal.SIGTERM)
            try:
                await asyncio.wait_for(process.wait(), WORKER_STOP_SECONDS)
            except TimeoutError:
                _log.warning("the worker process did not stop in %s s: killed", WORKER_STOP_SECONDS)
                process.kill()
                await process.wait()
        self._process = None

    async def _watch(self, process: asyncio.subprocess.Process) -> None:
        """Say so when the worker's process ends before `stop` asks it to: no turn runs from
        then on, until the server restarts."""
        code = await process.wait()
        _log.error("the worker process ended with code %s: no turn runs any more", code)
