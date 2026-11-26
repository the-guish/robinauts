# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The engine: a turn is one turn of a Codex thread, run by a ``codex app-server`` of its own;
a conversation is one or more Codex threads, under a working directory of its own.

The state is Codex's threads, under ``CODEX_HOME``, and nothing else: a checkpoint names the
thread and the turn the answer ended, so continuing is a resume of the thread, and continuing
from an earlier answer, as an edit or a regenerate does, is a fork of the thread through that
turn. A conversation's first turn starts a new thread. A turn that failed or was cancelled
stays in its thread; the next turn starts from the checkpoint before it, which forks the thread
and leaves it out. The threads a conversation made are listed in a file of the engine's, so that
``forget`` deletes them all, with the directory.

The app server runs for the turn alone, like the Claude Code worker's CLI: started with the
turn's configuration on its command line (config.py), and stopped when the turn ends, fails or
is cancelled. No question ever waits for a person: approvals are ``never``, and what Codex asks
anyway is declined.

``init_codex`` is the engine's factory, registered under the ``robinauts.engines`` entry-point
group as ``codex``. The storage it is handed is not used: the threads are the state, and the
worker is handed no database.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import uuid
from collections.abc import AsyncGenerator, Mapping
from pathlib import Path
from typing import Any

from robinauts.agent_engines.contract.domain import (
    AgentDefinition,
    CheckpointNotFoundError,
    EngineError,
    Event,
    FunctionTool,
    ProviderKind,
    SessionExistsError,
    SessionNotFoundError,
    UnknownModelError,
)
from robinauts.agent_engines.contract.ports import AgentEngine, EngineSettings, StorageConfig
from robinauts_codex_worker import checkpoints, config, tools
from robinauts_codex_worker.credentials import refused_text, subprocess_environment
from robinauts_codex_worker.events import Translation
from robinauts_codex_worker.rpc import (
    CLOSED,
    AppServer,
    Notification,
    RpcError,
    ServerClosed,
    ServerRequest,
)
from robinauts_codex_worker.settings import (
    HOME_VARIABLE,
    AuthMode,
    Settings,
    SystemPromptMode,
    settings_from,
)
from robinauts_worker_common.clone import GitError
from robinauts_worker_common.tasks import TaskPayloadError, code_task_starts
from robinauts_worker_common.worktrees import Repositories

_log = logging.getLogger(__name__)

ENGINE = "codex"
"""The engine's name in a configuration: ``[agents.<id>] engine = "codex"``."""

REPOSITORIES_FOLDER = "repos"
"""Where the clones of the flows' repositories are, under the workspaces: beside the sessions'
directories, whose names are ids and never this."""
THREADS_FOLDER = "robinauts-threads"
"""Under ``CODEX_HOME``: one file per conversation, the ids of the threads it made."""
INTERRUPT_SECONDS = 5.0
"""How long a cancelled or timed-out turn waits for Codex to stop before it is dropped."""
CLIENT = {"name": "robinauts", "title": "Robinauts", "version": "0.1.0"}
QUIET_NOTIFICATIONS = [
    "thread/tokenUsage/updated",
    "account/rateLimits/updated",
    "turn/diff/updated",
    "item/commandExecution/outputDelta",
]
"""What a turn does not need, left unsent. Usage counting is planned (docs/specs/core.md)."""
TURNS_PAGE = 100

DECLINED = {"decision": "decline"}
"""The answer to an approval Codex asks anyway: nobody is at the other end to give it."""


def init_codex(settings: EngineSettings, storage: StorageConfig) -> AgentEngine:
    """The engine, with the worker's own settings read from the environment: the entry point
    the worker module calls. ``SettingsError`` for an environment that is not this worker's,
    which ``__main__`` has refused before the worker starts."""
    container = settings_from(os.environ)
    root = container.workspaces / REPOSITORIES_FOLDER
    return CodexEngine(settings, container, Repositories.from_environ(root))


class CodexEngine(AgentEngine):
    def __init__(
        self,
        settings: EngineSettings,
        container: Settings,
        repositories: Repositories | None = None,
    ) -> None:
        """``repositories``: the clones a session gets a worktree of each of, in its directory,
        before its agent starts; none, a session's directory starts empty."""
        self._settings = settings
        self._container = container
        self._repositories = repositories

    @property
    def repositories(self) -> Repositories | None:
        return self._repositories

    def kinds(self) -> frozenset[ProviderKind]:
        if self._container.auth is AuthMode.PERSONAL:
            return frozenset({ProviderKind.OPENAI})
        return frozenset({ProviderKind.OPENAI, ProviderKind.OPENAI_COMPATIBLE})

    def workspace(self, session_id: uuid.UUID) -> Path:
        """The conversation's directory: Codex's ``cwd``."""
        return self._container.workspaces / str(session_id)

    def threads_file(self, session_id: uuid.UUID) -> Path:
        return self._container.home / THREADS_FOLDER / f"{session_id}.json"

    def threads(self, session_id: uuid.UUID) -> list[str]:
        """The threads the conversation made, oldest first."""
        try:
            listed = json.loads(self.threads_file(session_id).read_text())
        except (OSError, ValueError):
            return []
        return [str(thread) for thread in listed] if isinstance(listed, list) else []

    def _record(self, session_id: uuid.UUID, thread: str) -> None:
        path = self.threads_file(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        listed = self.threads(session_id)
        if thread not in listed:
            listed.append(thread)
        written = path.with_suffix(".tmp")
        written.write_text(json.dumps(listed))
        written.replace(path)

    async def setup(self) -> None:
        self._container.workspaces.mkdir(parents=True, exist_ok=True)
        (self._container.home / THREADS_FOLDER).mkdir(parents=True, exist_ok=True)

    async def create(self, session_id: uuid.UUID) -> None:
        workspace = self.workspace(session_id)
        if workspace.exists():
            raise SessionExistsError(f"session {session_id} exists")
        workspace.mkdir(parents=True)

    async def exists(self, session_id: uuid.UUID) -> bool:
        return self.workspace(session_id).is_dir()

    def task_starts(self, task_payload: Mapping[str, Any] | None) -> dict[str, str] | None:
        """The repositories a ``code_task/1`` payload starts the session's worktrees of, each
        with its branch; ``None`` for any other payload, or none. ``EngineError`` for a code
        task this worker cannot set up."""
        known = () if self._repositories is None else self._repositories.repositories
        try:
            return code_task_starts(task_payload, known)
        except TaskPayloadError as refused:
            raise EngineError(str(refused)) from None

    def command(self, arguments: list[str]) -> list[str]:
        return [self._container.cli_path, "app-server", *arguments]

    def environment(self, handed: Mapping[str, str]) -> dict[str, str]:
        """Codex's environment: the worker's, the turn's secrets, and its home."""
        return {**os.environ, **handed, HOME_VARIABLE: str(self._container.home)}

    async def _server(
        self, arguments: list[str], handed: Mapping[str, str], cwd: Path
    ) -> AppServer:
        try:
            server = await AppServer.open(self.command(arguments), self.environment(handed), cwd)
        except OSError as failed:
            raise EngineError(f"Codex could not be started: {failed}") from None
        try:
            await server.request(
                "initialize",
                {
                    "clientInfo": CLIENT,
                    "capabilities": {
                        "experimentalApi": True,
                        "optOutNotificationMethods": QUIET_NOTIFICATIONS,
                    },
                },
            )
            await server.notify("initialized")
        except (RpcError, ServerClosed) as failed:
            await server.close()
            raise EngineError(f"Codex did not start: {failed}") from None
        return server

    def thread_options(
        self, workspace: Path, agent: AgentDefinition, model_name: str
    ) -> dict[str, Any]:
        """What every thread of a turn is opened with, started, resumed or forked: the agent's
        prompt and model, and the turn's directory, policy and sandbox."""
        instructions = (
            "baseInstructions"
            if self._container.system_prompt is SystemPromptMode.CUSTOM
            else "developerInstructions"
        )
        return {
            "model": model_name,
            "cwd": str(workspace),
            "approvalPolicy": "never",
            "sandbox": self._container.sandbox,
            instructions: agent.system_prompt,
        }

    async def open_thread(
        self,
        server: AppServer,
        session_id: uuid.UUID,
        checkpoint_id: str | None,
        options: Mapping[str, Any],
        provider: str,
        functions: tuple[FunctionTool, ...],
    ) -> str:
        """The thread the turn runs on: a new one for a first turn; the checkpoint's thread when
        its turn is the thread's latest; a fork of that thread through its turn otherwise. A
        thread or a turn Codex does not hold is ``CheckpointNotFoundError``."""
        # Every call names the provider: a fork without it runs on Codex's default, ``openai``.
        options = {**options, "modelProvider": provider}
        if checkpoint_id is None:
            params: dict[str, Any] = dict(options)
            if functions:
                params["dynamicTools"] = tools.specs(functions)
            started = await server.request("thread/start", params)
            thread = str(started["thread"]["id"])
            self._record(session_id, thread)
            return thread
        checkpoint = checkpoints.decode(checkpoint_id)
        if checkpoint.thread not in self.threads(session_id):
            raise CheckpointNotFoundError(
                f"thread {checkpoint.thread} of checkpoint {checkpoint_id!r} is not this"
                " session's"
            )
        try:
            await server.request(
                "thread/resume", {**options, "threadId": checkpoint.thread, "excludeTurns": True}
            )
            turns = await self._turn_ids(server, checkpoint.thread, checkpoint.turn)
        except RpcError as refused:
            raise CheckpointNotFoundError(
                f"thread {checkpoint.thread} of checkpoint {checkpoint_id!r}: {refused.message}"
            ) from None
        if checkpoint.turn not in turns:
            raise CheckpointNotFoundError(
                f"thread {checkpoint.thread} holds no turn {checkpoint.turn} of checkpoint"
                f" {checkpoint_id!r}"
            )
        if turns[0] == checkpoint.turn:
            return checkpoint.thread
        forked = await server.request(
            "thread/fork",
            {**options, "threadId": checkpoint.thread, "lastTurnId": checkpoint.turn},
        )
        thread = str(forked["thread"]["id"])
        self._record(session_id, thread)
        return thread

    async def _turn_ids(self, server: AppServer, thread: str, wanted: str) -> list[str]:
        """The thread's turn ids, newest first, read until ``wanted`` is among them."""
        ids: list[str] = []
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {
                "threadId": thread,
                "limit": TURNS_PAGE,
                "sortDirection": "desc",
                "itemsView": "notLoaded",
            }
            if cursor:
                params["cursor"] = cursor
            page = await server.request("thread/turns/list", params)
            ids += [str(turn["id"]) for turn in page.get("data") or []]
            cursor = page.get("nextCursor")
            if wanted in ids or not cursor:
                return ids

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
            session_id, agent, prompt, model, checkpoint_id, timeout_seconds, task_payload
        )

    async def _stream(
        self,
        session_id: uuid.UUID,
        agent: AgentDefinition,
        prompt: str,
        model: str,
        checkpoint_id: str | None,
        timeout_seconds: float,
        task_payload: Mapping[str, Any] | None = None,
    ) -> AsyncGenerator[Event, None]:
        workspace = self.workspace(session_id)
        if not workspace.is_dir():
            raise SessionNotFoundError(f"session {session_id} was not created")
        model_config = self._settings.models.models.get(model)
        if model_config is None:
            raise UnknownModelError(f"model {model!r} is not configured")
        provider = self._settings.models.providers[model_config.provider]
        if provider.kind not in self.kinds():
            raise EngineError(f"provider {provider.id!r} is of a kind this mode cannot reach")
        if checkpoint_id is not None:
            checkpoints.decode(checkpoint_id)
        starts = self.task_starts(task_payload)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        if self._repositories is not None:
            # Before the agent: the session's worktrees, decided at its first turn, kept
            # between turns: a code task's repositories from its branches, else every one.
            try:
                async with asyncio.timeout_at(deadline):
                    await self._repositories.add_worktrees(
                        str(session_id), workspace, starts=starts, first=checkpoint_id is None
                    )
            except GitError as failed:
                raise EngineError(str(failed)) from None
        mode = self._container.auth
        arguments, headers = config.overrides(
            mode, agent, self._settings.models, self._settings.tool_secrets
        )
        handed = {**subprocess_environment(mode, provider, self._settings.keys), **headers}
        server = await self._server(arguments, handed, workspace)
        handlers: set[asyncio.Task[None]] = set()
        translation: Translation | None = None
        try:
            async with asyncio.timeout_at(deadline):
                options = self.thread_options(workspace, agent, model_config.name)
                thread = await self.open_thread(
                    server,
                    session_id,
                    checkpoint_id,
                    options,
                    config.provider_id(mode, provider),
                    agent.functions,
                )
                params = self.turn_params(thread, prompt, model_config.name)
                started = await server.request("turn/start", params)
            translation = Translation(thread, str(started["turn"]["id"]))
            while not translation.ended:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise TimeoutError
                try:
                    message = await asyncio.wait_for(server.queue.get(), remaining)
                except TimeoutError:
                    raise TimeoutError from None
                if message is CLOSED:
                    raise EngineError("Codex stopped before the turn ended")
                if isinstance(message, ServerRequest):
                    task = asyncio.create_task(self._answer(server, message, agent.functions))
                    handlers.add(task)
                    task.add_done_callback(handlers.discard)
                    continue
                assert isinstance(message, Notification)
                for event in translation.translate(message.method, message.params):
                    yield event
            failure = translation.failure()
            if failure is not None:
                # The turn's error is the operator's to read on the turn; the log says that
                # it happened, and why when the credential was refused, never what was said.
                if translation.credential_refused():
                    failure = refused_text(mode, provider)
                    _log.error(failure)
                else:
                    _log.warning("Codex ended a turn %s", translation.status)
                raise EngineError(failure)
            checkpoint = checkpoints.Checkpoint(translation.thread, translation.turn)
            yield translation.done(checkpoints.encode(checkpoint))
        except RpcError as refused:
            raise EngineError(f"Codex refused {refused.method}: {refused.message}") from None
        except ServerClosed:
            raise EngineError("Codex stopped before the turn ended") from None
        finally:
            if translation is not None and not translation.ended:
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    await asyncio.wait_for(
                        asyncio.shield(
                            server.request(
                                "turn/interrupt",
                                {"threadId": translation.thread, "turnId": translation.turn},
                            )
                        ),
                        INTERRUPT_SECONDS,
                    )
            for task in handlers:
                task.cancel()
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await asyncio.shield(server.close())

    def turn_params(self, thread: str, prompt: str, model_name: str) -> dict[str, Any]:
        params: dict[str, Any] = {
            "threadId": thread,
            "input": [{"type": "text", "text": prompt, "text_elements": []}],
            "model": model_name,
            "summary": self._container.summary,
        }
        if self._container.effort is not None:
            params["effort"] = self._container.effort
        return params

    async def _answer(
        self, server: AppServer, request: ServerRequest, functions: tuple[FunctionTool, ...]
    ) -> None:
        """Answer what Codex asks during a turn: a call of a tool run here with its output, an
        approval with a refusal, a question with no answer."""
        try:
            if request.method == "item/tool/call":
                await server.respond(request.id, await tools.call(functions, request.params))
            elif request.method in (
                "item/commandExecution/requestApproval",
                "item/fileChange/requestApproval",
            ):
                await server.respond(request.id, DECLINED)
            elif request.method == "item/tool/requestUserInput":
                await server.respond(request.id, {"answers": {}})
            else:
                await server.respond_error(
                    request.id, -32601, f"{request.method} is not answered by this worker"
                )
        except ServerClosed:
            return

    async def fork(self, source_id: uuid.UUID, target_id: uuid.UUID, *, checkpoint_id: str) -> None:
        raise NotImplementedError("the platform forks sessions by checkpoint, at the next turn")

    async def forget(self, session_id: uuid.UUID) -> None:
        workspace = self.workspace(session_id)
        if self._repositories is not None:
            try:
                await self._repositories.remove_worktrees(str(session_id), workspace)
            except GitError as failed:
                # The folder goes all the same; the clone prunes the worktree at its next fetch.
                _log.warning("the worktrees of session %s were not removed: %s", session_id, failed)
        threads = self.threads(session_id)
        if threads:
            await self._delete_threads(threads)
        self.threads_file(session_id).unlink(missing_ok=True)
        shutil.rmtree(workspace, ignore_errors=True)

    async def _delete_threads(self, threads: list[str]) -> None:
        """Delete the threads with Codex itself, newest first: a fork holds on to its source's
        history, so the source goes after it. A thread that cannot go is said in the log."""
        arguments, _ = config.overrides(
            self._container.auth,
            AgentDefinition(system_prompt=""),
            self._settings.models,
            self._settings.tool_secrets,
        )
        self._container.home.mkdir(parents=True, exist_ok=True)
        try:
            server = await self._server(arguments, {}, self._container.home)
        except EngineError as failed:
            _log.warning("the threads of a session were not deleted: %s", failed)
            return
        try:
            for thread in reversed(threads):
                try:
                    await server.request("thread/delete", {"threadId": thread})
                except RpcError as refused:
                    _log.warning("thread %s was not deleted: %s", thread, refused.message)
        except ServerClosed:
            _log.warning("Codex stopped while the threads of a session were deleted")
        finally:
            await server.close()
