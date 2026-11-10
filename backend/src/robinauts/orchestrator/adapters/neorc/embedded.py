# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""neorc, embedded in this server (``docs/architecture/orchestration.md``). This package is the
one place in Robinauts that imports neorc: take it and its wiring in ``cli.orchestration`` out and
the server is what it was without a flows folder.

- neorc's **manager** is an application inside this server's, mounted at ``/neorc`` with its UI
  at ``/neorc/ui/``, behind this server's own checks: the local development mode's ``Host`` and
  ``Origin`` rules reach it as they reach every path. It has no authentication of its own, as
  that mode has none. Its storage follows this server's: in this process without
  ``ROBINAUTS_DATABASE_URL``, else on that PostgreSQL, in a schema of its own, ``neorc``.
- the flows in ``flows`` are **uploaded** to it at every start;
- its **scheduler** runs in this process, on the manager directly;
- one **worker**, on the ``default`` queue, runs in a process of its own, with neorc's own
  command, against ``http://127.0.0.1:<port>/neorc``, and with the handlers at
  ``code_location``.

The worker is a process of its own, not a task in this one, for three reasons. neorc puts the
code location first on ``sys.path``, so a handler module could shadow a package this server
imports later. The handlers read ``ROBINAUTS_API_URL`` from their environment, and this
process's holds the model providers' keys and the database's URL, which a process of its own
is not handed. And a handler that blocks, or crashes its interpreter, takes a worker down, not
the server.

The UI is framed by the interface: its answers say ``frame-ancestors 'self'`` and
``SAMEORIGIN`` rather than neorc's ``'none'``, which this server's headers, set only where an
answer has none, then leave alone. Every other answer under ``/neorc`` carries this server's.
"""

from __future__ import annotations

import asyncio
import logging
import shlex
import signal
import sys
from collections.abc import Awaitable, Callable, Mapping, MutableMapping
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

from neorc.manager import create_app
from neorc_core import (
    FlowNotFoundError,
    InvalidValueError,
    Manager,
    NeorcError,
    Scheduler,
)
from neorc_core.flows import flow_files, load_flow_yaml, read_flows
from neorc_core.local import DirectManagerClient, MemoryStore, MemoryTaskNotifier

from robinauts.orchestrator.contract.domain import (
    FLOWS_VARIABLE,
    MOUNT,
    SETTINGS_FILE,
    ASGIApp,
    InvalidInputsError,
    NoSuchFlowError,
    OrchestrationConfig,
    OrchestrationStartError,
    Orchestrator,
)
from robinauts.orchestrator.ports.engine import FlowEngine
from robinauts.shared_domain.domain import Config

_log = logging.getLogger(__name__)

SCHEMA = "neorc"
"""The PostgreSQL schema neorc keeps its tables in, beside this server's."""

API_URL_VARIABLE = "ROBINAUTS_API_URL"
"""Where the handlers reach this server, handed to the worker."""
API_TOKEN_VARIABLE = "ROBINAUTS_API_TOKEN"
"""The API token the handlers send as a bearer when they have one: what a neorc started on its
own, against a server with sign-in on, is given (``docs/specs/sign-in.md``, "API tokens"). The
worker started here is handed none, whatever the shell had: this server takes none."""
SERVER_SECRETS = frozenset({"ROBINAUTS_DATABASE_URL", "ROBINAUTS_CONFIG"})
"""What the server's own environment holds that the worker is not handed, beside the variables
the configuration names."""

DEFAULT_QUEUE = "default"
POLL_SECONDS = 5.0
"""How long the scheduler waits in one poll; a stop cuts it short."""
STOP_SECONDS = 15.0
"""How long the stop waits for the worker after its SIGTERM, and for the scheduler, before it
kills or cancels them."""


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


class NeorcFlowEngine(FlowEngine):
    """Runs started on the manager in this process, as its own API would start them."""

    def __init__(self, manager: Manager) -> None:
        self._client = DirectManagerClient(manager)

    async def start_run(self, flow: str, inputs: Mapping[str, Any]) -> str:
        try:
            run = await self._client.start_run(flow, inputs)
        except FlowNotFoundError as error:
            raise NoSuchFlowError(str(error)) from error
        except InvalidValueError as error:
            raise InvalidInputsError(str(error)) from error
        return str(run.id)


def check_ready(
    orchestration: OrchestrationConfig, *, command: tuple[str, ...], ui: Path | None
) -> list[str]:
    """What stops the orchestrator from starting on this machine, before anything starts.
    ``ui`` is where neorc's built UI is, ``None`` when it is not there."""
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
    if orchestration.ui and ui is None:
        problems.append(
            f"{SETTINGS_FILE} ui: the installed neorc-ui holds no built UI; install its wheel, or"
            " set ui = false"
        )
    return problems


def built_ui() -> Path | None:
    """The directory of neorc's built UI, or ``None`` when the installed neorc-ui has none."""
    import neorc_ui

    try:
        return neorc_ui.static_dir()
    except FileNotFoundError:
        return None


def hidden_variables(config: Config) -> frozenset[str]:
    """The variables the worker is not handed: those the configuration reads a secret from,
    and the server's own."""
    named = {p.api_key_env for p in config.providers.values()}
    named |= {t.secret_env for t in config.tool_servers.values() if t.secret_env}
    return frozenset(named) | SERVER_SECRETS


def worker_environment(
    environ: Mapping[str, str], *, hidden: frozenset[str], api_url: str
) -> dict[str, str]:
    """The environment the worker starts with: the server's, without ``hidden`` and without an
    API token, and with this server's URL in ``ROBINAUTS_API_URL``."""
    environment = {k: v for k, v in environ.items() if k not in hidden}
    environment.pop(API_TOKEN_VARIABLE, None)
    environment[API_URL_VARIABLE] = api_url
    return environment


def worker_command(
    orchestration: OrchestrationConfig, *, command: tuple[str, ...], api_url: str
) -> tuple[str, ...]:
    """The worker's command: neorc's, against the manager mounted on this server."""
    return (
        *command,
        "worker",
        "start",
        "--manager-address",
        f"{api_url}{MOUNT}",
        "--code-location",
        str(orchestration.code_location),
        "--queue",
        DEFAULT_QUEUE,
    )


class Framable:
    """neorc's application, with the answers under its UI framable by this server's pages:
    ``frame-ancestors 'self'`` in the place of neorc's ``'none'``, and ``x-frame-options:
    SAMEORIGIN``. Nothing else changes, and no other path: the API's answers carry this
    server's headers, ``DENY`` included."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(
        self,
        scope: MutableMapping[str, Any],
        receive: Callable[[], Awaitable[Any]],
        send: Callable[[Any], Awaitable[None]],
    ) -> None:
        below = scope.get("path", "").removeprefix(scope.get("root_path", ""))
        if scope["type"] != "http" or not (below == "/ui" or below.startswith("/ui/")):
            await self.app(scope, receive, send)
            return

        async def framable(message: Any) -> None:
            if message["type"] == "http.response.start":
                headers = [
                    (name, _framable_policy(value) if name == b"content-security-policy" else value)
                    for name, value in message.get("headers", [])
                    if name != b"x-frame-options"
                ]
                headers.append((b"x-frame-options", b"SAMEORIGIN"))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, framable)


def _framable_policy(policy: bytes) -> bytes:
    directives = [d.strip() for d in policy.split(b";") if d.strip()]
    kept = [d for d in directives if not d.startswith(b"frame-ancestors")]
    return b"; ".join([*kept, b"frame-ancestors 'self'"])


class EmbeddedNeorc(Orchestrator):
    """neorc in this server: the manager's application, ``mounted``, to mount at ``MOUNT``; and
    ``start`` and ``stop`` for what runs beside it, around the server's own. ``api_url`` is where
    the worker reaches this server; ``database_url`` is this server's PostgreSQL, ``None`` for
    storage in this process."""

    def __init__(
        self,
        orchestration: OrchestrationConfig,
        environ: Mapping[str, str],
        *,
        hidden: frozenset[str],
        api_url: str,
        database_url: str | None,
        command: tuple[str, ...],
        ui: Path | None,
    ) -> None:
        self.orchestration = orchestration
        self.worker_command = worker_command(orchestration, command=command, api_url=api_url)
        self._worker_environment = worker_environment(environ, hidden=hidden, api_url=api_url)
        self._database_url = database_url
        self._stack = AsyncExitStack()
        self._open: Callable[[], Awaitable[None]] | None = None
        if database_url is None:
            self.manager = Manager(
                MemoryStore(), tasks=MemoryTaskNotifier(), events=MemoryTaskNotifier()
            )
        else:
            self.manager = self._on_postgres(database_url)
        self.app: ASGIApp = create_app(
            self.manager, access=None, ui=ui if orchestration.ui else None
        )
        self._mounted = Framable(self.app)
        self._scheduler: Scheduler | None = None
        self._scheduling: asyncio.Task[None] | None = None
        self._worker: asyncio.subprocess.Process | None = None
        self._watching: asyncio.Task[None] | None = None

    @property
    def mounted(self) -> ASGIApp:
        return self._mounted

    @property
    def engine(self) -> FlowEngine:
        """Where the flows' runs start: this manager."""
        return NeorcFlowEngine(self.manager)

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

    async def start(self) -> None:
        """Open the storage, upload the flows, start the scheduler and the worker. A start that
        fails, or is cancelled, part way stops what it had started before it raises."""
        try:
            await self._start()
        except BaseException:
            await self.stop()
            raise

    async def _start(self) -> None:
        if self._open is not None:
            try:
                await self._open()
            except (OSError, NeorcError) as error:
                raise OrchestrationStartError(f"neorc's storage did not open: {error}") from error
        await self._upload()
        self._scheduler = Scheduler(DirectManagerClient(self.manager), poll_timeout=POLL_SECONDS)
        self._scheduling = asyncio.create_task(self._schedule(self._scheduler), name="neorc")
        await self._spawn_worker()
        _log.info(
            "neorc is up: its manager at %s, its flows from %s, %s",
            MOUNT,
            self.orchestration.flows,
            "on PostgreSQL" if self._database_url else "in this process",
        )

    async def _upload(self) -> None:
        flows = self.orchestration.flows
        try:
            stored = await DirectManagerClient(self.manager).upload_flows(read_flows(flows))
        except (OSError, NeorcError) as error:
            raise OrchestrationStartError(f"neorc refused the flows in {flows}: {error}") from error
        _log.info("neorc flows from %s: %s new of %s", flows, sum(stored), len(stored))

    async def _schedule(self, scheduler: Scheduler) -> None:
        try:
            await scheduler.run()
        except asyncio.CancelledError:
            raise
        except BaseException:
            _log.exception("neorc's scheduler stopped: no run moves until the server restarts")

    async def _spawn_worker(self) -> None:
        command = self.worker_command
        _log.info("neorc's worker starts with the command: %s", shlex.join(command))
        try:
            self._worker = await asyncio.create_subprocess_exec(
                *command, env=self._worker_environment, stdin=asyncio.subprocess.DEVNULL
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
            shlex.join(self.worker_command),
        )

    async def stop(self) -> None:
        """Stop the worker, with SIGTERM and, after ``STOP_SECONDS``, killed; then the scheduler;
        then close the storage. Safe to repeat."""
        try:
            await self._stop_worker()
        finally:
            try:
                await self._stop_scheduler()
            finally:
                await self._stack.aclose()

    async def _stop_worker(self) -> None:
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

    async def _stop_scheduler(self) -> None:
        scheduling, self._scheduling = self._scheduling, None
        if self._scheduler is not None:
            self._scheduler.stop()
            self._scheduler = None
        if scheduling is None:
            return
        try:
            await asyncio.wait_for(asyncio.shield(scheduling), STOP_SECONDS)
        except TimeoutError:
            scheduling.cancel()
            await asyncio.gather(scheduling, return_exceptions=True)
