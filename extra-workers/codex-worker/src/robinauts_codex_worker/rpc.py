# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""A client of ``codex app-server``: Codex's JSON-RPC over its standard input and output, one
JSON object per line, without the ``jsonrpc`` field.

The client sends requests and awaits their results, and reads everything else the server sends
into one queue: notifications, and the server's own requests, which the caller answers with
``respond``. When the server's output ends, every pending request fails with ``ServerClosed``
and the queue gets ``CLOSED``. Its standard error goes to the debug log, since it may hold
what was said; it is never shown.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)

LINE_LIMIT = 64 * 1024 * 1024
"""The longest line read: a turn's items, a command's whole output, come in one line."""
STOP_SECONDS = 5.0
"""How long the server has to exit once its input is closed, before it is killed."""

CLOSED = object()
"""Put on the queue once the server's output ends."""


class RpcError(Exception):
    """A request the server answered with an error."""

    def __init__(self, method: str, code: int, message: str) -> None:
        super().__init__(f"{method}: {message} ({code})")
        self.method = method
        self.code = code
        self.message = message


class ServerClosed(Exception):
    """The server's output ended before it answered."""


@dataclass(frozen=True, slots=True)
class Notification:
    method: str
    params: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ServerRequest:
    id: Any
    method: str
    params: Mapping[str, Any]


class AppServer:
    """One ``codex app-server`` process, started by ``open`` and stopped by ``close``."""

    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self._process = process
        self._ids = itertools.count(1)
        self._pending: dict[Any, tuple[str, asyncio.Future[Any]]] = {}
        self.queue: asyncio.Queue[object] = asyncio.Queue()
        self._reader = asyncio.create_task(self._read())
        self._errors = asyncio.create_task(self._drain())

    @classmethod
    async def open(
        cls, command: Sequence[str], environment: Mapping[str, str], cwd: Path
    ) -> AppServer:
        """``OSError`` when the command cannot be run."""
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=dict(environment),
            cwd=cwd,
            limit=LINE_LIMIT,
        )
        return cls(process)

    async def _read(self) -> None:
        assert self._process.stdout is not None
        try:
            while line := await self._process.stdout.readline():
                try:
                    message = json.loads(line)
                except ValueError:
                    _log.debug("codex app-server wrote a line that is not JSON")
                    continue
                if isinstance(message, Mapping):
                    self._route(message)
        except (ValueError, OSError) as failed:
            _log.debug("codex app-server's output failed: %s", type(failed).__name__)
        finally:
            for _, future in self._pending.values():
                if not future.done():
                    future.set_exception(ServerClosed("codex app-server stopped"))
            self._pending.clear()
            self.queue.put_nowait(CLOSED)

    def _route(self, message: Mapping[str, Any]) -> None:
        method = message.get("method")
        params = message.get("params") or {}
        if method is None:
            pending = self._pending.pop(message.get("id"), None)
            if pending is None or pending[1].done():
                return
            requested, future = pending
            error = message.get("error")
            if error is not None:
                code = int(error.get("code", 0)) if isinstance(error, Mapping) else 0
                text = str(error.get("message", "")) if isinstance(error, Mapping) else str(error)
                future.set_exception(RpcError(requested, code, text))
            else:
                future.set_result(message.get("result") or {})
        elif "id" in message:
            self.queue.put_nowait(ServerRequest(message["id"], str(method), params))
        else:
            self.queue.put_nowait(Notification(str(method), params))

    async def _drain(self) -> None:
        assert self._process.stderr is not None
        with contextlib.suppress(ValueError, OSError):
            while line := await self._process.stderr.readline():
                _log.debug("codex: %s", line.decode(errors="replace").rstrip())

    async def _send(self, message: Mapping[str, Any]) -> None:
        assert self._process.stdin is not None
        if self._process.stdin.is_closing():
            raise ServerClosed("codex app-server's input is closed")
        try:
            self._process.stdin.write(json.dumps(message).encode() + b"\n")
            await self._process.stdin.drain()
        except (ConnectionError, OSError) as failed:
            raise ServerClosed(f"codex app-server's input failed: {failed}") from None

    async def request(self, method: str, params: Mapping[str, Any]) -> Mapping[str, Any]:
        """The result; ``RpcError`` for an error answer, ``ServerClosed`` for none."""
        id_ = next(self._ids)
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[id_] = (method, future)
        try:
            await self._send({"id": id_, "method": method, "params": params})
            return await future
        finally:
            self._pending.pop(id_, None)

    async def notify(self, method: str, params: Mapping[str, Any] | None = None) -> None:
        message: dict[str, Any] = {"method": method}
        if params is not None:
            message["params"] = params
        await self._send(message)

    async def respond(self, id_: Any, result: Mapping[str, Any]) -> None:
        await self._send({"id": id_, "result": result})

    async def respond_error(self, id_: Any, code: int, message: str) -> None:
        await self._send({"id": id_, "error": {"code": code, "message": message}})

    async def close(self) -> None:
        """Close its input, which ends the server; kill it if it lingers. Safe to repeat."""
        stdin = self._process.stdin
        if stdin is not None and not stdin.is_closing():
            stdin.close()
        try:
            await asyncio.wait_for(asyncio.shield(self._process.wait()), STOP_SECONDS)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                self._process.kill()
            await self._process.wait()
        for task in (self._reader, self._errors):
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(task, STOP_SECONDS)
