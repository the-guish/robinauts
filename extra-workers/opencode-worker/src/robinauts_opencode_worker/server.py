# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""One ``opencode serve`` for the worker, on loopback, and the HTTP client that talks to it.

The server listens on a port of this machine's loopback that the worker picks, behind a
password it makes at every start and hands to it alone, so no other process calls it. The
password is this process's, not a credential of anyone's, and dies with it. The server is
configured from the environment it is started with: ``OPENCODE_CONFIG_CONTENT``, which holds
the keys (config.py), and the switches below, which keep it from asking a person, reading a
project's own configuration, or reaching anything but the providers and the tool servers.

Its standard output and error go to the debug log, since they may hold what was said.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import secrets
import socket
from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import httpx

_log = logging.getLogger(__name__)

USER = "opencode"
CONFIG_VARIABLE = "OPENCODE_CONFIG_CONTENT"
PASSWORD_VARIABLE = "OPENCODE_SERVER_PASSWORD"
LISTENING = re.compile(r"listening on (http://\S+)")
STOP_SECONDS = 5.0

LOCKED_DOWN: Mapping[str, str] = {
    # No question tool: nobody is at the other end to answer it.
    "OPENCODE_CLIENT": "sdk",
    # Nothing fetched or uploaded: the models' catalogue is the one the binary carries.
    "OPENCODE_DISABLE_MODELS_FETCH": "1",
    "OPENCODE_DISABLE_AUTOUPDATE": "1",
    "OPENCODE_DISABLE_LSP_DOWNLOAD": "1",
    "OPENCODE_DISABLE_EMBEDDED_WEB_UI": "1",
    # The logins' plugins: this worker runs on API keys alone (CREDENTIALS.md).
    "OPENCODE_DISABLE_DEFAULT_PLUGINS": "1",
    # A repository the agent works on may hold a configuration of its own; it is not read.
    "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
    "OPENCODE_DISABLE_CLAUDE_CODE": "1",
    "OPENCODE_DISABLE_EXTERNAL_SKILLS": "1",
}
"""OpenCode's switches for a server nobody sits in front of (docs/specs/core.md, "Nothing phones
home")."""


class ServerError(Exception):
    """``opencode serve`` did not start, or stopped."""


def free_port(host: str = "127.0.0.1") -> int:
    """A port of loopback nothing listens on now: ``opencode serve --port 0`` tries 4096 first,
    which another worker's server may hold."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((host, 0))
        return int(probe.getsockname()[1])


class OpenCodeServer:
    """Started by ``start``, stopped by ``stop``; ``client`` reaches it while it runs."""

    def __init__(self, process: asyncio.subprocess.Process, url: str, password: str) -> None:
        self._process = process
        self.url = url
        self.client = httpx.AsyncClient(
            base_url=url,
            auth=httpx.BasicAuth(USER, password),
            # A synchronous prompt holds its answer until the turn ends; nothing here waits on
            # one, but the event stream stays open as long as the turn.
            timeout=httpx.Timeout(30.0, read=None),
        )
        self._drains = [
            asyncio.create_task(_drain(stream, name))
            for stream, name in ((process.stdout, "stdout"), (process.stderr, "stderr"))
            if stream is not None
        ]

    @classmethod
    async def start(
        cls,
        cli_path: str,
        configuration: Mapping[str, Any],
        environment: Mapping[str, str],
        cwd: Path,
        start_seconds: float,
    ) -> OpenCodeServer:
        """``ServerError`` when it cannot be run, or does not say it listens in time."""
        password = secrets.token_urlsafe(32)
        host = "127.0.0.1"
        command: Sequence[str] = [
            cli_path,
            "serve",
            "--hostname",
            host,
            "--port",
            str(free_port(host)),
        ]
        env = {
            **environment,
            **LOCKED_DOWN,
            CONFIG_VARIABLE: json.dumps(configuration),
            PASSWORD_VARIABLE: password,
        }
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=cwd,
            )
        except OSError as failed:
            raise ServerError(f"OpenCode could not be started: {failed}") from None
        try:
            url = await asyncio.wait_for(_listening(process), start_seconds)
        except (TimeoutError, ServerError) as failed:
            await _stop(process)
            said = "it said nothing in time" if isinstance(failed, TimeoutError) else str(failed)
            raise ServerError(f"OpenCode did not start: {said}") from None
        return cls(process, url, password)

    @property
    def running(self) -> bool:
        return self._process.returncode is None

    async def events(self, directory: Path) -> AsyncIterator[tuple[str, Mapping[str, Any]]]:
        """The server's events for one directory, ``(type, properties)``, as they come: its
        ``/event`` stream, parsed. Ends when the server ends it."""
        async with self.client.stream(
            "GET",
            "/event",
            params={"directory": str(directory)},
            headers={"Accept": "text/event-stream"},
        ) as response:
            response.raise_for_status()
            data: list[str] = []
            async for line in response.aiter_lines():
                if line.startswith("data:"):
                    data.append(line[5:].lstrip())
                    continue
                if line or not data:
                    continue
                try:
                    event = json.loads("\n".join(data))
                except ValueError:
                    event = None
                data = []
                if isinstance(event, Mapping) and isinstance(event.get("type"), str):
                    yield event["type"], event.get("properties") or {}

    async def stop(self) -> None:
        with contextlib.suppress(Exception):
            await self.client.aclose()
        await _stop(self._process)
        for task in self._drains:
            task.cancel()
            with contextlib.suppress(BaseException):
                await task


async def _listening(process: asyncio.subprocess.Process) -> str:
    """The address the server says it listens on, read from its standard output."""
    assert process.stdout is not None
    while line := await process.stdout.readline():
        text = line.decode(errors="replace")
        if found := LISTENING.search(text):
            return found.group(1).rstrip("/")
        _log.debug("opencode: %s", text.rstrip())
    raise ServerError(f"it exited with {await process.wait()}")


async def _drain(stream: asyncio.StreamReader, name: str) -> None:
    with contextlib.suppress(ValueError, OSError):
        while line := await stream.readline():
            _log.debug("opencode %s: %s", name, line.decode(errors="replace").rstrip())


async def _stop(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        process.terminate()
    try:
        await asyncio.wait_for(process.wait(), STOP_SECONDS)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        await process.wait()
