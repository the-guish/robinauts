# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The start and the stop of what the composition built, in order: what ``robinauts start``
runs around the HTTP server (``cli/main.py``), and what a test runs around an app or in its
place. There is a worker for each the configuration names (``AgentConfig.worker``), the
default first. They run in this process, or each in a process of its own, which reaches the
core through this server's API alone (``broker_url``), with a secret of its own handed to it
at its start on its standard input, never in its environment: the secret says which worker
asks, and the API hands it the turns of its own agents alone. A worker whose table names a
command (``Composed.commands``) is started with that command in place of this build's worker
module, in a process of its own even where the rest run in this one, and differs in nothing
else: the same environment, the same secret, the same line to say it is ready, the same
SIGTERM to stop. That is how a worker runs in a container, on another machine, or with an
engine this build lacks."""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import shlex
import signal
import sys
import tempfile
import time
from pathlib import Path
from typing import cast

from robinauts.cli.composition import Composed
from robinauts.cli.worker_process import (
    BROKER_URL_VARIABLE,
    PARENT_VARIABLE,
    READY_LINE,
    WORKER_NAME_VARIABLE,
)
from robinauts.shared_domain.domain import DEFAULT_WORKER
from robinauts.web.changes import FeedGate
from robinauts.web.worker_api import WorkerAccess

_log = logging.getLogger(__name__)

WORKER_MODULE = "robinauts.cli.worker_process"


class WorkerStartError(RuntimeError):
    """A worker's process could not be started, ended, or said nothing for
    ``WORKER_START_SECONDS``, before its worker started."""


WORKER_START_SECONDS = 60.0
"""How long `start` waits for a worker's process to say its worker has started, before it
kills it and fails."""

HEALTHY_WITHIN = 300.0
"""Seconds: a worker that has not touched its activity file for longer is unhealthy."""

WORKER_STOP_SECONDS = 15.0
"""How long `stop` waits for a worker's process after its SIGTERM before it kills it: more
than the worker waits for its tasks."""


def activity_file(worker: str) -> Path:
    """The file that says when this server last heard from its worker ``worker``: the default's
    is named after the server's process alone, the others after their worker too. A worker in
    this process touches it at every heartbeat; for one in a process of its own, the server
    touches it at every request the worker makes."""
    suffix = "" if worker == DEFAULT_WORKER else f"-{worker}"
    return Path(tempfile.gettempdir()) / f"robinauts-worker-{os.getpid()}{suffix}.alive"


class Lifecycle:
    def __init__(
        self, composed: Composed, *, broker_url: str | None = None, in_process: bool | None = None
    ) -> None:
        """``broker_url`` is where a worker in a process of its own reaches the core: this
        server's, as the worker's machine reaches it. ``in_process`` says whether the workers
        without a command of their own run in this process rather than in one each; left out,
        they do when there is no URL. That suits a store in this process, which is this
        process's alone: an engine in another process would hold nothing of a session this one
        ran. A worker with a command runs in a process of its own either way, so it needs the
        URL."""
        self.composed = composed
        self._broker_url = broker_url or ""
        self._in_process = broker_url is None if in_process is None else in_process
        self._spawned = tuple(
            name for name in composed.workers if not self._in_process or name in composed.commands
        )
        if self._spawned and not self._broker_url:
            raise ValueError(
                f"the workers {', '.join(self._spawned)} run in processes of their own, which"
                " reach the core at a broker_url"
            )
        self._workers_running = False
        self._opened = False
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._watching: dict[str, asyncio.Task[None]] = {}
        self.access = WorkerAccess(heard=self._heard)
        self.feed = FeedGate()
        """Closed by the server's shut-down, which ends the change feed's waits."""
        """What lets the workers' processes in through the API."""
        self.activity_files = {name: activity_file(name) for name in composed.workers}
        """When each worker was last heard from, by worker: the file's time."""

    async def start(self) -> None:
        """Open the controller, then start every worker. Return once they all have started, so
        that a server which says it is up runs turns. Each worker process is let in through the
        API from then on, with the secret it read from its standard input at its start.

        A start that fails, or is cancelled, part way stops what it had started before it
        raises: the workers, in this process or their own, and the controller."""
        try:
            await self._start()
        except BaseException:
            await self.stop()
            raise

    async def _start(self) -> None:
        await self.composed.controller.open()
        self._opened = True
        # Before they start: one that fails part way holds what it opened until stopped.
        self._workers_running = True
        if self._spawned:
            # Somebody is served over the API. Every worker's broker reaches the one store,
            # which tells each listener every cancel.
            await self.composed.brokers[next(iter(self.composed.brokers))].listen_for_cancels(
                self.access.stop_requested
            )
        for name, worker in self.composed.workers.items():
            if name not in self._spawned:
                worker.activity_file = self.activity_files[name]
                await worker.start()
        # Side by side: a server is up once its slowest worker is. One that failed to start
        # fails the start, and `start` then stops the others.
        spawning = [self._spawn(name, self._broker_url) for name in self._spawned]
        failed = [
            result
            for result in await asyncio.gather(*spawning, return_exceptions=True)
            if isinstance(result, BaseException)
        ]
        if failed:
            raise failed[0]

    def command_of(self, name: str) -> tuple[str, ...]:
        """What starts the process of the worker ``name``: its table's command, or this build's
        worker module."""
        return self.composed.commands.get(name) or (sys.executable, "-m", WORKER_MODULE)

    async def _spawn(self, name: str, broker_url: str) -> None:
        """Start the process of the worker ``name``, and return once its worker has started."""
        secret = secrets.token_urlsafe(32)
        self.access.open(secret, name)
        environment = {
            **os.environ,
            PARENT_VARIABLE: str(os.getpid()),
            BROKER_URL_VARIABLE: broker_url,
            WORKER_NAME_VARIABLE: name,
        }
        # The secret, whole and closed, on the process's standard input before it exists: far
        # shorter than a pipe holds, and read to its end at once.
        read_end, write_end = os.pipe()
        try:
            os.write(write_end, secret.encode())
        finally:
            os.close(write_end)
        command = self.command_of(name)
        try:
            process = await asyncio.create_subprocess_exec(
                *command, env=environment, stdin=read_end, stdout=asyncio.subprocess.PIPE
            )
        except OSError as error:
            raise WorkerStartError(
                f"the command of worker {name!r} could not be started: {error}"
            ) from error
        finally:
            os.close(read_end)
        # Once it exists: a stop then finds it, whatever fails next.
        self._processes[name] = process
        await self._wait_until_ready(name, process)
        # Heard from, as of now: its requests keep the file's time from here on.
        self.activity_files[name].touch()
        self._watching[name] = asyncio.create_task(
            self._watch(name, process), name=f"worker process {name}"
        )

    async def _wait_until_ready(self, name: str, process: asyncio.subprocess.Process) -> None:
        """Wait for the worker's line on its standard output, logging whatever else it says
        first, or stop its process and fail: it ended first, or it took longer than
        ``WORKER_START_SECONDS``."""
        output = cast(asyncio.StreamReader, process.stdout)
        deadline = asyncio.get_running_loop().time() + WORKER_START_SECONDS
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            try:
                line = await asyncio.wait_for(output.readline(), max(remaining, 0.0))
            except TimeoutError:
                why = f"did not start within {WORKER_START_SECONDS:g} s"
                break
            if line == READY_LINE:
                return
            if not line:
                why = "ended before its worker started"
                break
            _said(name, line)
        await self._stop_process(name)
        started_with = shlex.join(self.command_of(name))
        raise WorkerStartError(
            f"the process of worker {name!r} {why} (started with: {started_with})"
        )

    def _heard(self, name: str) -> None:
        """A request from the worker ``name``, through the API: for a worker in a process of
        its own, the sign of life the health check reads, as a worker in this process gives it
        by touching the file itself."""
        if name in self._processes:
            self.activity_files[name].touch()

    def healthy(self) -> bool:
        """Whether each of this node's workers has been heard from within ``HEALTHY_WITHIN``."""
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
            stops = [
                worker.stop()
                for name, worker in self.composed.workers.items()
                if name not in self._spawned
            ]
            await asyncio.gather(*stops, *(self._stop_process(n) for n in list(self._processes)))
            # Once stopped: a stop that failed or was cancelled is done again by the next one.
            self._workers_running = False
        self.access.close()

    async def stop(self) -> None:
        """Stop the workers, if they still run, then close the controller, if it is open. Each
        is marked done only once it is, so a stop that failed or was cancelled part way is
        finished by the next; and once all is done, a stop awaits nothing, so a cancellation
        still pending cannot cut it short."""
        await self.stop_worker()
        for activity_file in self.activity_files.values():
            activity_file.unlink(missing_ok=True)
        if self._opened:
            await self.composed.controller.close()
            self._opened = False
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
                await asyncio.wait_for(_ended(name, process), WORKER_STOP_SECONDS)
            except TimeoutError:
                _log.warning(
                    "the process of worker %r did not stop in %s s: killed",
                    name,
                    WORKER_STOP_SECONDS,
                )
                process.kill()
                await process.wait()

    async def _watch(self, name: str, process: asyncio.subprocess.Process) -> None:
        """Log what a worker's process says, and say so when it ends before `stop` asks it to:
        no turn of its agents runs from then on, until the server restarts."""
        code = await _ended(name, process)
        _log.error(
            "the process of worker %r ended with code %s: no turn of its agents runs any more"
            " (it was started with: %s)",
            name,
            code,
            shlex.join(self.command_of(name)),
        )


async def _ended(name: str, process: asyncio.subprocess.Process) -> int:
    """The process's exit code once it has ended, what it said on its standard output logged
    line by line meanwhile, so that it is never held up by a pipe nobody reads."""
    output = cast(asyncio.StreamReader, process.stdout)
    while line := await output.readline():
        _said(name, line)
    return await process.wait()


def _said(name: str, line: bytes) -> None:
    _log.info("worker %r says: %s", name, line.decode(errors="replace").rstrip())
