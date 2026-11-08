# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The orchestrator ``robinauts start`` runs beside itself in the local development mode:
neorc's manager, its scheduler and one worker, each a process of its own started with neorc's
own command (``docs/architecture/orchestration.md``). Nothing here imports neorc: the
``[orchestration]`` table of the configuration names the command that runs it, and neorc is
installed on its own.

The order is neorc's: the manager first, on the loopback interface and with no authentication,
as the local development mode is; once it answers ``/health``, the flows are uploaded to it; then
the scheduler, and the worker, which runs the flows' tasks with the handlers at
``code_location``. The worker alone is handed this server's URL, in ``ROBINAUTS_API_URL``: that
is how a task reaches this server's API. It is handed no credential: the local development mode
has no sign-in, every request is its one user's, and what a flow starts is the developer's, as
the browser's is. The stop goes the other way round.

None of neorc's processes is handed what the configuration keeps secret from everything but
the server: the model providers' keys, the tool servers' secrets, the database's URL. neorc's
own database is the PostgreSQL the variable ``database_url_env`` names, handed to it as
``NEORC_DATABASE_URL``."""

from __future__ import annotations

import asyncio
import logging
import shlex
import signal
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from robinauts.controller.contract.domain import ConfigError
from robinauts.shared_domain.domain import Config

_log = logging.getLogger(__name__)

TABLE = "orchestration"
"""The configuration's table that turns the orchestrator on."""

KEYS = frozenset({"command", "port", "database_url_env", "code_location", "flows", "ui"})

API_URL_VARIABLE = "ROBINAUTS_API_URL"
"""Where the handlers reach this server, handed to the worker."""
API_TOKEN_VARIABLE = "ROBINAUTS_API_TOKEN"
"""The API token the handlers send as a bearer when they have one: what a neorc started on its
own, against a server with sign-in on, is given (``docs/specs/sign-in.md``, "API tokens"). The
worker started here is handed none, whatever the shell had: this server takes none."""
NEORC_DATABASE_URL_VARIABLE = "NEORC_DATABASE_URL"
SERVER_SECRETS = frozenset({"ROBINAUTS_DATABASE_URL", "ROBINAUTS_CONFIG"})
"""What the server's own environment holds that no neorc process is handed, beside the
variables the configuration names."""

HOST = "127.0.0.1"
DEFAULT_PORT = 8420
DEFAULT_QUEUE = "default"

MANAGER_START_SECONDS = 60.0
"""How long `start` waits for neorc's manager to answer ``/health``."""
UPLOAD_SECONDS = 60.0
STOP_SECONDS = 15.0
"""How long `stop` waits for each process after its SIGTERM before it kills it."""

DEV_ONLY = (
    f"[{TABLE}]: neorc is started with no authentication, which only the local development"
    " mode allows for now: start with --dev-no-sign-in, or take the table out"
)


class OrchestrationStartError(RuntimeError):
    """One of neorc's processes could not be started, ended before it was ready, or did not
    get ready in time."""


@dataclass(frozen=True, slots=True)
class OrchestrationConfig:
    """The ``[orchestration]`` table, read: paths are absolute."""

    code_location: Path
    flows: Path
    command: tuple[str, ...] = ("neorc",)
    port: int = DEFAULT_PORT
    database_url_env: str = NEORC_DATABASE_URL_VARIABLE
    ui: bool = True

    @property
    def manager_address(self) -> str:
        return f"http://{HOST}:{self.port}"


def parse_orchestration(
    table: Mapping[str, Any], *, base: Path, default_code: Path
) -> OrchestrationConfig:
    """The ``[orchestration]`` table, with its relative paths taken from ``base``, the
    configuration file's directory. ``ConfigError`` names every problem."""
    problems = [f"[{TABLE}] {key}: unknown key" for key in sorted(table.keys() - KEYS)]
    command = table.get("command", ["neorc"])
    if (
        not isinstance(command, list)
        or not command
        or not all(isinstance(part, str) and part for part in command)
    ):
        problems.append(f"[{TABLE}] command: a non-empty list of non-empty strings")
        command = ["neorc"]
    port = table.get("port", DEFAULT_PORT)
    if isinstance(port, bool) or not isinstance(port, int) or not 0 < port < 65536:
        problems.append(f"[{TABLE}] port: a port number, 1 to 65535")
        port = DEFAULT_PORT
    database_url_env = table.get("database_url_env", NEORC_DATABASE_URL_VARIABLE)
    if not isinstance(database_url_env, str) or not database_url_env:
        problems.append(f"[{TABLE}] database_url_env: the name of an environment variable")
        database_url_env = NEORC_DATABASE_URL_VARIABLE
    ui = table.get("ui", True)
    if not isinstance(ui, bool):
        problems.append(f"[{TABLE}] ui: true or false")
        ui = True
    paths: dict[str, Path] = {}
    for key in ("code_location", "flows"):
        value = table.get(key)
        if value is not None and (not isinstance(value, str) or not value):
            problems.append(f"[{TABLE}] {key}: a path")
            value = None
        if value is not None:
            paths[key] = base / value
    code_location = paths.get("code_location", default_code)
    flows = paths.get("flows", code_location / "flows")
    if problems:
        raise ConfigError("\n".join(problems))
    return OrchestrationConfig(
        code_location=code_location,
        flows=flows,
        command=tuple(command),
        port=port,
        database_url_env=database_url_env,
        ui=ui,
    )


def check_ready(orchestration: OrchestrationConfig, environ: Mapping[str, str]) -> list[str]:
    """What stops the orchestrator from starting on this machine, before anything starts."""
    problems = []
    if not environ.get(orchestration.database_url_env):
        problems.append(
            f"[{TABLE}]: no database for neorc: set {orchestration.database_url_env} to the"
            " PostgreSQL its manager keeps its flows and runs in"
        )
    where = {"code_location": orchestration.code_location, "flows": orchestration.flows}
    for what, path in where.items():
        if not path.is_dir():
            problems.append(f"[{TABLE}] {what}: no directory at {path}")
    return problems


def hidden_variables(config: Config) -> frozenset[str]:
    """The variables no neorc process is handed: those the configuration reads a secret from,
    and the server's own."""
    named = {p.api_key_env for p in config.providers.values()}
    named |= {t.secret_env for t in config.tool_servers.values() if t.secret_env}
    return frozenset(named) | SERVER_SECRETS


def neorc_environment(
    environ: Mapping[str, str],
    orchestration: OrchestrationConfig,
    *,
    hidden: frozenset[str],
    api_url: str | None = None,
) -> dict[str, str]:
    """The environment a neorc process starts with: the server's, without ``hidden`` and
    without an API token, with neorc's database in ``NEORC_DATABASE_URL``; and for the worker,
    this server's URL in ``ROBINAUTS_API_URL``."""
    environment = {k: v for k, v in environ.items() if k not in hidden}
    environment.pop(API_URL_VARIABLE, None)
    environment.pop(API_TOKEN_VARIABLE, None)
    environment[NEORC_DATABASE_URL_VARIABLE] = environ[orchestration.database_url_env]
    if api_url is not None:
        environment[API_URL_VARIABLE] = api_url
    return environment


def neorc_commands(orchestration: OrchestrationConfig) -> dict[str, tuple[str, ...]]:
    """The command of each step, by name, in the order they run."""
    neorc = orchestration.command
    address = ("--manager-address", orchestration.manager_address)
    manager = (*neorc, "manager", "start", "--host", HOST, "--port", str(orchestration.port))
    manager += ("--no-auth", "--create-schema")
    if not orchestration.ui:
        manager += ("--no-ui",)
    return {
        "manager": manager,
        "upload": (*neorc, "flows", "upload", str(orchestration.flows), *address),
        "scheduler": (*neorc, "scheduler", "start", *address),
        "worker": (
            *neorc,
            "worker",
            "start",
            *address,
            "--code-location",
            str(orchestration.code_location),
            "--queue",
            DEFAULT_QUEUE,
        ),
    }


class Orchestration:
    """neorc's manager, scheduler and worker, started in that order and stopped the other way
    round. ``api_url`` is where the worker reaches this server."""

    def __init__(
        self,
        orchestration: OrchestrationConfig,
        environ: Mapping[str, str],
        *,
        hidden: frozenset[str],
        api_url: str,
    ) -> None:
        self.orchestration = orchestration
        self.commands = neorc_commands(orchestration)
        self._environment = neorc_environment(environ, orchestration, hidden=hidden)
        self._worker_environment = neorc_environment(
            environ, orchestration, hidden=hidden, api_url=api_url
        )
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._watching: dict[str, asyncio.Task[None]] = {}

    async def start(self) -> None:
        """Start the manager, upload the flows, start the scheduler and the worker. A start
        that fails, or is cancelled, part way stops what it had started before it raises."""
        try:
            await self._start()
        except BaseException:
            await self.stop()
            raise

    async def _start(self) -> None:
        manager = await self._spawn("manager", self._environment)
        await self._wait_until_healthy(manager)
        await self._upload()
        await self._spawn("scheduler", self._environment)
        await self._spawn("worker", self._worker_environment)
        for name, process in self._processes.items():
            self._watching[name] = asyncio.create_task(
                self._watch(name, process), name=f"neorc {name}"
            )
        _log.info(
            "neorc is up: its manager at %s, its flows from %s",
            self.orchestration.manager_address,
            self.orchestration.flows,
        )

    async def _spawn(self, name: str, environment: dict[str, str]) -> asyncio.subprocess.Process:
        command = self.commands[name]
        _log.info("neorc %s starts with the command: %s", name, shlex.join(command))
        try:
            process = await asyncio.create_subprocess_exec(
                *command, env=environment, stdin=asyncio.subprocess.DEVNULL
            )
        except OSError as error:
            raise OrchestrationStartError(
                f"neorc's {name} could not be started with {shlex.join(command)}: {error}"
            ) from error
        self._processes[name] = process
        return process

    async def _wait_until_healthy(self, manager: asyncio.subprocess.Process) -> None:
        """Until the manager answers ``/health``; fail when it ends first, or takes longer
        than ``MANAGER_START_SECONDS``."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + MANAGER_START_SECONDS
        url = f"{self.orchestration.manager_address}/health"
        while loop.time() < deadline:
            if manager.returncode is not None:
                raise OrchestrationStartError(
                    f"neorc's manager ended with code {manager.returncode} before it answered"
                    f" (started with: {shlex.join(self.commands['manager'])})"
                )
            if await asyncio.to_thread(_answers, url):
                return
            await asyncio.sleep(0.2)
        raise OrchestrationStartError(
            f"neorc's manager did not answer {url} within {MANAGER_START_SECONDS:g} s"
        )

    async def _upload(self) -> None:
        command = self.commands["upload"]
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                env=self._environment,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except OSError as error:
            raise OrchestrationStartError(
                f"the flows could not be uploaded with {shlex.join(command)}: {error}"
            ) from error
        try:
            said, _ = await asyncio.wait_for(process.communicate(), UPLOAD_SECONDS)
        except TimeoutError:
            process.kill()
            await process.wait()
            raise OrchestrationStartError(
                f"uploading the flows took longer than {UPLOAD_SECONDS:g} s"
            ) from None
        text = said.decode(errors="replace").strip()
        if process.returncode != 0:
            raise OrchestrationStartError(
                f"neorc refused the flows in {self.orchestration.flows}"
                f" (code {process.returncode}): {text}"
            )
        for line in text.splitlines():
            _log.info("neorc flows: %s", line)

    async def stop(self) -> None:
        """Stop the worker, the scheduler and the manager, in that order, each with SIGTERM
        and, after ``STOP_SECONDS``, killed. Safe to repeat."""
        for name in ("worker", "scheduler", "manager"):
            await self._stop_process(name)

    async def _stop_process(self, name: str) -> None:
        watching = self._watching.pop(name, None)
        if watching is not None:
            watching.cancel()
            await asyncio.gather(watching, return_exceptions=True)
        process = self._processes.pop(name, None)
        if process is None or process.returncode is not None:
            return
        process.send_signal(signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), STOP_SECONDS)
        except TimeoutError:
            _log.warning("neorc's %s did not stop in %s s: killed", name, STOP_SECONDS)
            process.kill()
            await process.wait()

    async def _watch(self, name: str, process: asyncio.subprocess.Process) -> None:
        code = await process.wait()
        _log.error(
            "neorc's %s ended with code %s: the orchestrator is down until the server restarts"
            " (it was started with: %s)",
            name,
            code,
            shlex.join(self.commands[name]),
        )


def _answers(url: str) -> bool:
    """Whether ``url`` answers 200 now."""
    try:
        with urllib.request.urlopen(url, timeout=2) as answer:  # noqa: S310 -- loopback http
            return bool(answer.status == 200)
    except (urllib.error.URLError, OSError):
        return False
