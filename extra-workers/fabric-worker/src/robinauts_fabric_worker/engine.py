# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The engine: a turn is one invocation of a NeMo Fabric runtime of the Claude adapter, which
drives Claude Code; a conversation is one Fabric runtime, kept alive between its turns, under a
working directory of its own.

Fabric returns a turn whole, so the chat shows the answer, its tool calls and their results at
the turn's end, not as they are written. The Claude adapter keeps the conversation's memory in
the runtime's Claude Code process alone and resumes nothing, so the engine's own state is the turn
log (history.py): a checkpoint is a turn there. A turn that continues the live runtime's latest
answer is invoked on it. Any other turn starts a runtime of its own, seeded with the conversation
up to its checkpoint, which ends the live one: a conversation's first turn, an edit or a
regenerate under an earlier answer, a turn on another model or agent definition than the live
runtime's, and a turn after the runtime was stopped, by the worker's restart, its idle time or
the cap on live runtimes. A turn that fails, times out or is cancelled ends its runtime too: what
it did is not in the log, and the next turn starts again from its checkpoint.

``init_fabric`` is the engine's factory, registered under the ``robinauts.engines`` entry-point
group as ``fabric``. The storage the worker hands it is not used: the state is in the worker's
state folder, and the worker is handed no database.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import sys
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from robinauts.agent_engines.contract.domain import (
    AgentDefinition,
    Done,
    EngineError,
    Event,
    ProviderKind,
    SessionNotFoundError,
    UnknownModelError,
)
from robinauts.agent_engines.contract.ports import AgentEngine, EngineSettings, StorageConfig
from robinauts_fabric_worker import events, fabric_config
from robinauts_fabric_worker.history import TurnLog
from robinauts_fabric_worker.settings import Settings, settings_from

_log = logging.getLogger(__name__)

ENGINE = "fabric"
"""The engine's name in a configuration: ``[agents.<id>] engine = "fabric"``."""

ADAPTER_PYTHON_VARIABLE = "ADAPTER_PYTHON"
"""The interpreter Fabric runs its Python adapter hosts with: this worker's own, which has the
Claude adapter installed."""
SESSIONS_FOLDER = "sessions"
RUNTIMES_FOLDER = "runtimes"
STOP_SECONDS = 10.0
"""How long the engine waits for a runtime to stop before it lets go of it."""


class FabricRuntime(Protocol):
    """What the engine uses of a ``nemo_fabric.Runtime``."""

    async def invoke(self, *, input: Any = None) -> Any: ...

    async def stop(self) -> None: ...


Starter = Callable[[Any, Path], Awaitable[FabricRuntime]]
"""Starts a runtime from a ``FabricConfig`` and its base folder."""


async def start_fabric_runtime(config: Any, base_dir: Path) -> FabricRuntime:
    """A runtime of Fabric's own, on a client of its own, so no client's record of runtimes
    grows past Fabric's limit."""
    from nemo_fabric import Fabric

    return await Fabric().start_runtime(config, base_dir=base_dir)


def init_fabric(settings: EngineSettings, storage: StorageConfig) -> AgentEngine:
    """The engine, with the worker's own settings read from the environment: the entry point
    the worker module calls. ``SettingsError`` for an environment that is not this worker's,
    which ``__main__`` has refused before the worker starts."""
    os.environ.setdefault(ADAPTER_PYTHON_VARIABLE, sys.executable)
    return FabricEngine(settings, settings_from(os.environ))


@dataclass(slots=True)
class Live:
    runtime: FabricRuntime
    signature: str
    head: str | None
    """The checkpoint the runtime's memory ends at: the turn it answered last."""
    last_used: float
    busy: bool = False


@dataclass(slots=True)
class _Pool:
    live: dict[uuid.UUID, Live] = field(default_factory=dict)


class FabricEngine(AgentEngine):
    def __init__(
        self,
        settings: EngineSettings,
        worker: Settings,
        starter: Starter = start_fabric_runtime,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._settings = settings
        self._worker = worker
        self._start = starter
        self._clock = clock
        self._log = TurnLog(worker.state / SESSIONS_FOLDER)
        self._pool = _Pool()

    def kinds(self) -> frozenset[ProviderKind]:
        return frozenset({ProviderKind.ANTHROPIC, ProviderKind.ANTHROPIC_COMPATIBLE})

    def workspace(self, session_id: uuid.UUID) -> Path:
        """The conversation's directory: Claude Code's ``cwd``."""
        return self._worker.workspaces / str(session_id)

    def live(self, session_id: uuid.UUID) -> Live | None:
        return self._pool.live.get(session_id)

    def _now(self) -> float:
        return self._clock() if self._clock is not None else asyncio.get_running_loop().time()

    async def setup(self) -> None:
        self._worker.workspaces.mkdir(parents=True, exist_ok=True)
        (self._worker.state / RUNTIMES_FOLDER).mkdir(parents=True, exist_ok=True)
        self._log.setup()

    async def create(self, session_id: uuid.UUID) -> None:
        self._log.create(session_id)
        self.workspace(session_id).mkdir(parents=True, exist_ok=True)

    async def exists(self, session_id: uuid.UUID) -> bool:
        return self._log.exists(session_id)

    def stream(
        self,
        session_id: uuid.UUID,
        agent: AgentDefinition,
        prompt: str,
        *,
        model: str,
        checkpoint_id: str | None,
        timeout_seconds: float,
        resume: bool = False,
        task_payload: Mapping[str, Any] | None = None,
    ) -> AsyncGenerator[Event, None]:
        return self._stream(
            session_id, agent, prompt, model, checkpoint_id, timeout_seconds, resume
        )

    async def _stream(
        self,
        session_id: uuid.UUID,
        agent: AgentDefinition,
        prompt: str,
        model: str,
        checkpoint_id: str | None,
        timeout_seconds: float,
        resume: bool,
    ) -> AsyncGenerator[Event, None]:
        session = self._log.load(session_id)
        chain = session.chain(checkpoint_id)
        model_config = self._settings.models.models.get(model)
        if model_config is None:
            raise UnknownModelError(f"model {model!r} is not configured")
        provider = self._settings.models.providers[model_config.provider]
        if provider.kind not in self.kinds():
            raise EngineError(f"provider {provider.id!r} is of a kind Claude Code cannot reach")
        if resume and session.latest is not None:
            latest = session.turns[session.latest]
            if latest.parent == checkpoint_id:
                if latest.prompt != prompt:
                    raise EngineError("the turn to resume asked another question")
                # It had finished: its answer again, as it was.
                for data in latest.events:
                    yield events.decode(data)
                yield Done(latest.answer, latest.id)
                return
        if agent.functions:
            _log.debug(
                "the agent's %d in-process tools are not offered behind Fabric",
                len(agent.functions),
            )
        workspace = self.workspace(session_id)
        if not workspace.is_dir():
            raise SessionNotFoundError(f"session {session_id} has no workspace")
        planned = fabric_config.runtime(
            agent=agent,
            model_id=model,
            settings=self._settings,
            worker=self._worker,
            workspace=workspace,
            artifacts=self._worker.state / RUNTIMES_FOLDER / str(session_id),
            chain=chain,
        )
        async with asyncio.timeout(timeout_seconds):
            live = await self._runtime_for(session_id, agent, planned, checkpoint_id, workspace)
            live.busy = True
            try:
                result = await live.runtime.invoke(input=prompt)
            except BaseException:
                await self._end(session_id)
                raise
            finally:
                live.busy = False
                live.last_used = self._now()
        mapping = result.to_mapping() if hasattr(result, "to_mapping") else dict(result)
        failure = events.failure(mapping)
        if failure is not None:
            await self._end(session_id)
            if events.credential_refused(mapping):
                failure = (
                    f"Anthropic refused the API key of provider {provider.id!r}, read from"
                    f" {provider.api_key_env}: check it and replace the value"
                )
                _log.error(failure)
            else:
                _log.warning("Claude Code behind Fabric ended a turn badly: %s", failure)
            raise EngineError(failure)
        turn_events, text = events.translate(mapping)
        turn = self._log.add(
            session_id, checkpoint_id, prompt, text, [events.encode(e) for e in turn_events]
        )
        live.head = turn.id
        for event in turn_events:
            yield event
        yield Done(text, turn.id)

    async def _runtime_for(
        self,
        session_id: uuid.UUID,
        agent: AgentDefinition,
        planned: fabric_config.Runtime,
        checkpoint_id: str | None,
        workspace: Path,
    ) -> Live:
        """The live runtime when its memory ends at the checkpoint and it was started the same
        way; else a new one, seeded, in place of it."""
        live = self._pool.live.get(session_id)
        if (
            live is not None
            and checkpoint_id is not None
            and live.head == checkpoint_id
            and live.signature == planned.signature
        ):
            return live
        await self._end(session_id)
        await self._make_room()
        fabric_config.write_permissions(
            workspace, fabric_config.permissions(agent, self._settings, self._worker)
        )
        base = self._worker.state / RUNTIMES_FOLDER / str(session_id)
        base.mkdir(parents=True, exist_ok=True)
        runtime = await self._start(planned.config, base)
        live = Live(runtime, planned.signature, checkpoint_id, self._now())
        self._pool.live[session_id] = live
        return live

    async def _make_room(self) -> None:
        """Stop the runtimes idle past their time, then the least recently used idle ones while
        the live ones are at the cap, so that the next one fits."""
        now = self._now()
        idle = [
            (live.last_used, session_id)
            for session_id, live in self._pool.live.items()
            if not live.busy
        ]
        idle.sort()
        for last_used, session_id in idle:
            over_cap = len(self._pool.live) >= self._worker.max_runtimes
            if now - last_used >= self._worker.idle_seconds or over_cap:
                await self._end(session_id)

    async def _end(self, session_id: uuid.UUID) -> None:
        """Stop the session's live runtime, if it has one, bounded; the next turn starts
        another."""
        live = self._pool.live.pop(session_id, None)
        if live is None:
            return
        try:
            async with asyncio.timeout(STOP_SECONDS):
                await asyncio.shield(live.runtime.stop())
        except (Exception, asyncio.CancelledError) as error:
            if isinstance(error, asyncio.CancelledError):
                # Cancelled while stopping: the stop goes on in the background.
                raise
            _log.warning("a Fabric runtime did not stop cleanly: %s", type(error).__name__)

    async def fork(self, source_id: uuid.UUID, target_id: uuid.UUID, *, checkpoint_id: str) -> None:
        raise NotImplementedError("the platform forks sessions by checkpoint, at the next turn")

    async def forget(self, session_id: uuid.UUID) -> None:
        await self._end(session_id)
        self._log.forget(session_id)
        shutil.rmtree(self.workspace(session_id), ignore_errors=True)
        shutil.rmtree(self._worker.state / RUNTIMES_FOLDER / str(session_id), ignore_errors=True)

    async def close(self) -> None:
        for session_id in list(self._pool.live):
            with contextlib.suppress(Exception):
                await self._end(session_id)
