# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The start and the stop of what the composition built, in order: what ``robinauts start``
runs around the HTTP server (``cli/main.py``), and what a test runs around an app or in its
place. There is a worker for each the configuration names (``AgentConfig.worker``), the
default first. They run in this process, or each in a process of its own (``broker_url``),
which reaches the core through this server's API alone, with a secret of its own handed to it
at its start through a pipe, never its environment: the secret says which worker asks, and
the API hands it the turns of its own agents alone."""

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
    WORKER_NAME_VARIABLE,
)
from robinauts.shared_domain.domain import DEFAULT_WORKER
from robinauts.web.worker_api import WorkerAccess

_log = logging.getLogger(__name__)

WORKER_MODULE = "robinauts.cli.worker_process"


class WorkerStartError(RuntimeError):
    """A worker's process ended, or said nothing for ``WORKER_START_SECONDS``, before its
    worker started."""


WORKER_START_SECONDS = 60.0
"""How long `start` waits for a worker's process to say its worker has started, before it
kills it and fails."""

HEALTHY_WITHIN = 300.0
"""Seconds: a worker that has not touched its activity file for longer is unhealthy."""

WORKER_STOP_SECONDS = 15.0
"""How long `stop` waits for a worker's process after its SIGTERM before it kills it: more
than the worker waits for its tasks."""


def activity_file(worker: str) -> Path:
    """The file this server's worker ``worker`` touches at every heartbeat: the default's
    is named after the server's process alone, the others after their worker too."""
    suffix = "" if worker == DEFAULT_WORKER else f"-{worker}"
    return Path(tempfile.gettempdir()) / f"robinauts-worker-{os.getpid()}{suffix}.alive"


class Lifecycle:
    def __init__(self, composed: Composed, *, broker_url: str | None = None) -> None:
        """``broker_url`` runs each worker in a process of its own, which reaches the core at
        that URL: this server's, as the worker's machine reaches it. That suits PostgreSQL
        alone, where the engines' state is in the database: an engine in this process would
        hold nothing of a session another process ran."""
        self.composed = composed
        self._broker_url = broker_url
        self._workers_running = False
        self._opened = False
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._watching: dict[str, asyncio.Task[None]] = {}
        self.access = WorkerAccess()
        """What lets the workers' processes in through the API, while they run."""
        self.activity_files = {name: activity_file(name) for name in composed.workers}
        """What each of this node's workers touches at every heartbeat, by worker, in this
        process or its own."""

    async def start(self) -> None:
        """Open the controller, then start every worker. Return once they all have started, so
        that a server which says it is up runs turns. Each worker process is let in through the
        API from then on, with the secret it reads from a pipe at its start."""
        await self.composed.controller.open()
        self._opened = True
        if self._broker_url is None:
            self._workers_running = True
            for name, worker in self.composed.workers.items():
                worker.activity_file = self.activity_files[name]
                await worker.start()
            return
        self._workers_running = True
        await self.composed.broker.listen_for_cancels(self.access.stop_requested)
        try:
            # Side by side: a server is up once its slowest worker is.
            spawning = [self._spawn(name, self._broker_url) for name in self.composed.workers]
            failed = [
                result
                for result in await asyncio.gather(*spawning, return_exceptions=True)
                if isinstance(result, BaseException)
            ]
            if failed:
                raise failed[0]
        except BaseException:
            await self.stop_worker()
            raise

    async def _spawn(self, name: str, broker_url: str) -> None:
        """Start the process of the worker ``name``, and return once its worker has started."""
        secret = secrets.token_urlsafe(32)
        self.access.open(secret, name)
        environment = {
            **os.environ,
            PARENT_VARIABLE: str(os.getpid()),
            ACTIVITY_VARIABLE: str(self.activity_files[name]),
            BROKER_URL_VARIABLE: broker_url,
            WORKER_NAME_VARIABLE: name,
        }
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
                process = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-m",
                    WORKER_MODULE,
                    env=environment,
                    pass_fds=(write_end, secret_end),
                )
            finally:
                os.close(write_end)
                os.close(secret_end)
            self._processes[name] = process
            await self._wait_until_ready(name, process, read_end)
        finally:
            os.close(read_end)
        self._watching[name] = asyncio.create_task(
            self._watch(name, process), name=f"worker process {name}"
        )

    async def _wait_until_ready(
        self, name: str, process: asyncio.subprocess.Process, ready: int
    ) -> None:
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
            await self._stop_process(name)
            raise WorkerStartError(
                f"the process of worker {name!r} did not start within"
                f" {WORKER_START_SECONDS:g} s"
            )

    def healthy(self) -> bool:
        """Whether each of this node's workers has touched its activity file within
        ``HEALTHY_WITHIN``."""
        now = time.time()
        for activity_file in self.activity_files.values():
            try:
                touched = activity_file.stat().st_mtime
            except FileNotFoundError:
                return False
            if now - touched > HEALTHY_WITHIN:
                return False
        return True

    async def stop_worker(self) -> None:
        """Stop every worker, each waiting for the turns it runs, unless they are stopped
        already. A server does it while it still listens: a worker in a process of its own
        ends its turns through the API. Then the API lets no worker in."""
        if self._workers_running:
            self._workers_running = False
            if self._broker_url is None:
                await asyncio.gather(*(w.stop() for w in self.composed.workers.values()))
            else:
                await asyncio.gather(*(self._stop_process(n) for n in list(self._processes)))
        self.access.close()

    async def stop(self) -> None:
        """Stop the workers, if they still run, then close the controller, if it is open.
        Again, or after a start that failed, it does only what is left, and awaits nothing once
        all is done: a stop a signal has cancelled already must not be cut short by it."""
        await self.stop_worker()
        for activity_file in self.activity_files.values():
            activity_file.unlink(missing_ok=True)
        if self._opened:
            self._opened = False
            await self.composed.controller.close()
            _log.info("the controller is closed")

    async def _stop_process(self, name: str) -> None:
        watching = self._watching.pop(name, None)
        if watching is not None:
            watching.cancel()
            await asyncio.gather(watching, return_exceptions=True)
        process = self._processes.pop(name, None)
        if process is not None and process.returncode is None:
            process.send_signal(signal.SIGTERM)
            try:
                await asyncio.wait_for(process.wait(), WORKER_STOP_SECONDS)
            except TimeoutError:
                _log.warning(
                    "the process of worker %r did not stop in %s s: killed",
                    name,
                    WORKER_STOP_SECONDS,
                )
                process.kill()
                await process.wait()

    async def _watch(self, name: str, process: asyncio.subprocess.Process) -> None:
        """Say so when a worker's process ends before `stop` asks it to: no turn of its agents
        runs from then on, until the server restarts."""
        code = await process.wait()
        _log.error(
            "the process of worker %r ended with code %s: no turn of its agents runs any more",
            name,
            code,
        )
