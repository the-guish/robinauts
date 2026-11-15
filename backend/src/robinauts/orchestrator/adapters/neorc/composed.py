# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""neorc, composed into this backend (``docs/architecture/orchestration.md``). This package is the
one place in Robinauts that imports neorc, beside ``cli.orchestration``, which wires it.

- neorc's **manager** is a oneberth component, named ``neorc`` and served under ``/neorc/``
  beside Robinauts' own. oneberth checks every request to it for its credential and its neorc
  permission, as it checks Robinauts' routes. Its worker routes are on the worker listener.
  Its storage follows this backend's: in this process without a database, a SQLite file
  ``neorc.db`` in the data directory, or a schema of its own, ``neorc``, on the PostgreSQL.
- its **scheduler** runs in this process, a part of the component.
- once the backend serves, the flows of the flows folder are **uploaded** with a service token
  of ``neorc/ci``, after the ``default`` queue is declared as a worker queue. This backend is
  the operator here, and declares the queue on the manager directly.
- one **worker**, on the ``default`` queue, runs in a process of its own with neorc's own
  command. It reaches the worker listener with a token of ``neorc/worker`` bound to that queue,
  and its handlers reach Robinauts' API on the front listener with a token of
  ``robinauts/handler``.
- a run a person starts through Robinauts is started on neorc's API with that person's own
  credential, in this process, through the backend's own front listener.

The worker is a process of its own, not a task in this one, for three reasons. neorc puts the
code location first on ``sys.path``, so a handler module could shadow a package this server
imports later. The handlers read ``ROBINAUTS_API_URL`` from their environment, and this
process's holds the model providers' keys and the database's URL, which a process of its own
is not handed. And a handler that blocks, or crashes its interpreter, takes a worker down, not
the server.
"""

from __future__ import annotations

import asyncio
import logging
import shlex
import signal
import sys
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, cast

import httpx
from neorc.http import HttpManagerClient
from neorc.manager import new_manager_webapp
from neorc_core import (
    FlowNotFoundError,
    InvalidValueError,
    Manager,
    ManagerUnavailableError,
    NeorcError,
    QueueKind,
)
from neorc_core.flows import flow_files, load_flow_yaml, read_flows
from neorc_core.local import MemoryStore, MemoryTaskNotifier

from robinauts.orchestrator.contract.domain import (
    FLOWS_VARIABLE,
    MOUNT,
    SETTINGS_FILE,
    Flows,
    InvalidInputsError,
    NoSuchFlowError,
    OrchestrationConfig,
    OrchestrationStartError,
    Orchestrator,
    TokenMinter,
)
from robinauts.orchestrator.ports.engine import FlowEngine
from robinauts.shared_domain.domain import Config

_log = logging.getLogger(__name__)

SCHEMA = "neorc"
"""The PostgreSQL schema neorc keeps its tables in, beside this server's."""
FILE_NAME = "neorc.db"
"""neorc's SQLite file, in the data directory."""

API_URL_VARIABLE = "ROBINAUTS_API_URL"
"""Where the handlers reach Robinauts' API, handed to the worker: the component's address on
the front listener."""
API_TOKEN_VARIABLE = "ROBINAUTS_API_TOKEN"
"""The token the handlers send as a bearer: a service token of ``robinauts/handler``."""
NEORC_TOKEN_VARIABLE = "NEORC_API_TOKEN"
"""The token neorc's worker sends to the manager: a service token of ``neorc/worker``."""
SERVER_SECRETS = frozenset(
    {
        "ROBINAUTS_DATABASE_URL",
        "ROBINAUTS_CONFIG",
        "ONEBERTH_CONFIG",
        NEORC_TOKEN_VARIABLE,
    }
)
"""What the server's own environment holds that the worker is not handed, beside the variables
the configuration names."""

DEFAULT_QUEUE = "default"
STOP_SECONDS = 15.0
"""How long the stop waits for the worker after its SIGTERM before it kills it."""
SERVED_SECONDS = 30.0
"""How long the start waits for the backend to answer neorc's API in this process."""

UPLOAD_TOKEN = "robinauts-flows"
WORKER_TOKEN = "robinauts-neorc-worker"
HANDLER_TOKEN = "robinauts-handlers"
CI_ROLE = "neorc/ci"
WORKER_ROLE = "neorc/worker"
HANDLER_ROLE = "robinauts/handler"


def neorc_command() -> tuple[str, ...]:
    """neorc's own command, as installed beside this interpreter with neorc."""
    return (str(Path(sys.executable).with_name("neorc")),)


def declared_inputs(folder: Path) -> dict[str, dict[str, str]]:
    """By flow name, the inputs each flow file in ``folder`` declares and their types, in the
    file's order. A file neorc would refuse is left out: the upload refuses it, with neorc's
    reason, before anything starts."""
    declared = {}
    for path in flow_files(folder):
        try:
            definition = load_flow_yaml(path.read_text(encoding="utf-8"))
        except (OSError, NeorcError):
            continue
        declared[definition.name] = {name: str(kind) for name, kind in definition.inputs.items()}
    return declared


def check_ready(orchestration: OrchestrationConfig, *, command: tuple[str, ...]) -> list[str]:
    """What stops the orchestrator from starting on this machine, before anything starts."""
    problems = []
    if not orchestration.flows.is_dir():
        problems.append(f"{FLOWS_VARIABLE}: no directory at {orchestration.flows}")
    if not orchestration.code_location.is_dir():
        problems.append(
            f"{SETTINGS_FILE} code_location: no directory at {orchestration.code_location}"
        )
    if not Path(command[0]).is_file():
        problems.append(
            f"{FLOWS_VARIABLE}: no neorc command at {command[0]} to run its worker with"
        )
    return problems


def hidden_variables(config: Config) -> frozenset[str]:
    """The variables the worker is not handed: those the configuration reads a secret from,
    and the server's own."""
    named = {p.api_key_env for p in config.providers.values()}
    named |= {t.secret_env for t in config.tool_servers.values() if t.secret_env}
    return frozenset(named) | SERVER_SECRETS


def worker_environment(
    environ: Mapping[str, str],
    *,
    hidden: frozenset[str],
    api_url: str,
    api_token: str,
    neorc_token: str,
) -> dict[str, str]:
    """The environment the worker starts with: the server's, without ``hidden``, with this
    backend's API in ``ROBINAUTS_API_URL`` and the two tokens."""
    environment = {k: v for k, v in environ.items() if k not in hidden}
    environment[API_URL_VARIABLE] = api_url
    environment[API_TOKEN_VARIABLE] = api_token
    environment[NEORC_TOKEN_VARIABLE] = neorc_token
    return environment


def worker_command(
    orchestration: OrchestrationConfig, *, command: tuple[str, ...], manager_url: str
) -> tuple[str, ...]:
    """The worker's command: neorc's, against the manager on this backend's worker listener."""
    return (
        *command,
        "worker",
        "start",
        "--manager-address",
        manager_url,
        "--code-location",
        str(orchestration.code_location),
        "--queue",
        DEFAULT_QUEUE,
    )


def neorc_roles() -> object:
    """neorc's role table, as its component gives it to oneberth."""
    manager = Manager(MemoryStore(), tasks=MemoryTaskNotifier(), events=MemoryTaskNotifier())
    return new_manager_webapp(manager=manager).roles


class NeorcFlowEngine(FlowEngine):
    """Runs started on neorc's API, in this process, with the credential of whoever asks."""

    def __init__(self, public_url: str, transport: httpx.AsyncBaseTransport) -> None:
        self._public_url = public_url
        self._transport = transport

    async def start_run(
        self, flow: str, inputs: Mapping[str, Any], *, credential: str | None
    ) -> str:
        client = HttpManagerClient(
            self._public_url,
            token=credential,
            allow_insecure=True,
            transport=self._transport,
        )
        async with client:
            try:
                run = await client.start_run(flow, dict(inputs))
            except FlowNotFoundError as error:
                raise NoSuchFlowError(str(error)) from error
            except InvalidValueError as error:
                raise InvalidInputsError(str(error)) from error
        return str(run.id)


class ComposedNeorc(Orchestrator):
    """neorc in this backend: its manager on storage of this backend's kind, the component
    that serves it, and the flows folder's upload and worker, when there is a folder.

    ``public_url`` is the backend's, which its own requests to neorc's API name; ``api_url``
    the front listener's, where the handlers reach Robinauts' API; ``worker_url`` the worker
    listener's, where neorc's worker reaches the manager. Name at most one place
    for neorc's records: ``database_url`` or ``database_file``; with neither, it keeps them in
    this process."""

    def __init__(
        self,
        orchestration: OrchestrationConfig | None,
        environ: Mapping[str, str],
        *,
        hidden: frozenset[str],
        public_url: str,
        api_url: str,
        worker_url: str,
        database_url: str | None = None,
        database_file: Path | None = None,
        command: tuple[str, ...] = (),
        make_flows: Callable[[FlowEngine], Flows] | None = None,
    ) -> None:
        self.orchestration = orchestration
        self._environ = dict(environ)
        self._hidden = hidden
        self._public_url = public_url
        self._api_url = api_url
        self._command = command
        self._make_flows = make_flows
        self._flows: Flows | None = None
        self.worker_command = (
            None
            if orchestration is None
            else worker_command(orchestration, command=command, manager_url=worker_url)
        )
        self._stack = AsyncExitStack()
        self._open: Callable[[], Awaitable[None]] | None = None
        self._where = "in this process"
        if database_url is not None:
            self.manager = self._on_postgres(database_url)
            self._where = "on PostgreSQL"
        elif database_file is not None:
            self.manager = self._on_sqlite(database_file)
            self._where = f"in {database_file}"
        else:
            self.manager = Manager(
                MemoryStore(), tasks=MemoryTaskNotifier(), events=MemoryTaskNotifier()
            )
        self._component = new_manager_webapp(manager=self.manager, scheduler=True)
        self._worker: asyncio.subprocess.Process | None = None
        self._watching: asyncio.Task[None] | None = None

    @property
    def component(self) -> object:
        return self._component

    def flows(self, transport: object) -> Flows | None:
        if self._make_flows is None or self.orchestration is None:
            return None
        if self._flows is None:
            engine = NeorcFlowEngine(self._public_url, cast(httpx.AsyncBaseTransport, transport))
            self._flows = self._make_flows(engine)
        return self._flows

    def _on_postgres(self, database_url: str) -> Manager:
        from neorc.postgres import PostgresStore, PostgresTaskNotifier, create_schema
        from neorc.postgres._notifier import EVENTS_CHANNEL, TASKS_CHANNEL

        store = PostgresStore(database_url, schema=SCHEMA)
        tasks = PostgresTaskNotifier(database_url, channel=TASKS_CHANNEL)
        events = PostgresTaskNotifier(database_url, channel=EVENTS_CHANNEL)

        async def open_() -> None:
            await create_schema(database_url, schema=SCHEMA)
            for part in (store, tasks, events):
                await self._stack.enter_async_context(part)

        self._open = open_
        return Manager(store, tasks=tasks, events=events)

    def _on_sqlite(self, path: Path) -> Manager:
        from neorc.sqlite import SqliteStore

        store = SqliteStore(path)

        async def open_() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            await self._stack.enter_async_context(store)

        self._open = open_
        # SQLite has no notifications across processes: the manager runs in this one alone.
        return Manager(store, tasks=MemoryTaskNotifier(), events=MemoryTaskNotifier())

    async def open(self) -> None:
        if self._open is None:
            return
        try:
            await self._open()
        except (OSError, RuntimeError, NeorcError) as error:
            await self._stack.aclose()
            raise OrchestrationStartError(f"neorc's storage did not open: {error}") from error
        _log.info("neorc's manager keeps its records %s", self._where)

    async def close(self) -> None:
        await self._stack.aclose()

    async def start(self, mint: TokenMinter, transport: object) -> None:
        if self.orchestration is None:
            return
        try:
            await self._start(mint, cast(httpx.AsyncBaseTransport, transport))
        except BaseException:
            await self.stop()
            raise

    async def _start(self, mint: TokenMinter, transport: httpx.AsyncBaseTransport) -> None:
        orchestration = cast(OrchestrationConfig, self.orchestration)
        await self._upload(orchestration, await mint(UPLOAD_TOKEN, CI_ROLE, None), transport)
        await self._spawn_worker(
            api_token=await mint(HANDLER_TOKEN, HANDLER_ROLE, None),
            neorc_token=await mint(WORKER_TOKEN, WORKER_ROLE, DEFAULT_QUEUE),
        )
        _log.info(
            "neorc is up: its manager at %s, its flows from %s, its records %s",
            MOUNT,
            orchestration.flows,
            self._where,
        )

    async def _upload(
        self,
        orchestration: OrchestrationConfig,
        token: str,
        transport: httpx.AsyncBaseTransport,
    ) -> None:
        """Declare the worker's queue, then upload the flows with ``token``. neorc refuses a
        flow on a queue nobody declared. A kind already declared is kept."""
        flows = orchestration.flows
        try:
            if await self.manager.queue_kind(DEFAULT_QUEUE) is None:
                await self.manager.declare_queue(DEFAULT_QUEUE, QueueKind.WORKER)
            contents = read_flows(flows)
            client = HttpManagerClient(
                self._public_url, token=token, allow_insecure=True, transport=transport
            )
            async with client:
                stored = await _when_served(lambda: client.upload_flows(contents))
        except (OSError, NeorcError) as error:
            raise OrchestrationStartError(f"neorc refused the flows in {flows}: {error}") from error
        _log.info("neorc flows from %s: %s new of %s", flows, sum(stored), len(stored))

    async def _spawn_worker(self, *, api_token: str, neorc_token: str) -> None:
        command = cast(tuple[str, ...], self.worker_command)
        environment = worker_environment(
            self._environ,
            hidden=self._hidden,
            api_url=f"{self._api_url}/robinauts",
            api_token=api_token,
            neorc_token=neorc_token,
        )
        _log.info("neorc's worker starts with the command: %s", shlex.join(command))
        try:
            self._worker = await asyncio.create_subprocess_exec(
                *command, env=environment, stdin=asyncio.subprocess.DEVNULL
            )
        except OSError as error:
            raise OrchestrationStartError(
                f"neorc's worker could not be started with {shlex.join(command)}: {error}"
            ) from error
        self._watching = asyncio.create_task(self._watch(self._worker), name="neorc worker")

    async def _watch(self, process: asyncio.subprocess.Process) -> None:
        code = await process.wait()
        _log.error(
            "neorc's worker ended with code %s: no task runs until the server restarts"
            " (it was started with: %s)",
            code,
            shlex.join(self.worker_command or ()),
        )

    async def stop(self) -> None:
        """Stop the worker, with SIGTERM and, after ``STOP_SECONDS``, killed. Safe to repeat."""
        watching, self._watching = self._watching, None
        if watching is not None:
            watching.cancel()
            await asyncio.gather(watching, return_exceptions=True)
        process, self._worker = self._worker, None
        if process is None or process.returncode is not None:
            return
        process.send_signal(signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), STOP_SECONDS)
        except TimeoutError:
            _log.warning("neorc's worker did not stop in %s s: killed", STOP_SECONDS)
            process.kill()
            await process.wait()


async def _when_served[T](ask: Callable[[], Awaitable[T]]) -> T:
    """What ``ask`` answers once the backend serves neorc's API: it answers 503 while it is
    still starting, which neorc's client reads as a manager that is not there yet."""
    deadline = asyncio.get_running_loop().time() + SERVED_SECONDS
    while True:
        try:
            return await ask()
        except ManagerUnavailableError:
            if asyncio.get_running_loop().time() > deadline:
                raise
            await asyncio.sleep(0.1)
