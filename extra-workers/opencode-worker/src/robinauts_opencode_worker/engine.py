# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The engine: a turn is one prompt of an OpenCode session, on the worker's one ``opencode
serve``; a conversation is one or more OpenCode sessions, in a directory of its own.

The state is OpenCode's sessions, in its own database under ``XDG_DATA_HOME``, and nothing
else: a checkpoint names the session and the last assistant message of the turn, so continuing
is a prompt of that session, and continuing from an earlier answer, as an edit or a regenerate
does, is a fork of the session that keeps the messages up to that answer. A conversation's
first turn creates a new session. A turn that failed or was cancelled stays in its session; the
next turn starts from the checkpoint before it, which forks the session and leaves it out. The
sessions a conversation made are listed in a file of the engine's, so that ``forget`` deletes
them all, with the directory.

The server is started at ``setup`` with every configured provider and tool server, and stopped
at ``close``; one that stopped is started again at the next turn. Which tool servers a turn's
agent may use is the session's permission rules. The tools run in the worker's process are
served by an MCP server of the worker's on loopback, under a token of the turn's, which each
turn adds to its directory.

``init_opencode`` is the engine's factory, registered under the ``robinauts.engines``
entry-point group as ``opencode``. The storage it is handed is not used: the sessions are the
state, and the worker is handed no database.
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

import httpx

from robinauts.agent_engines.contract.domain import (
    AgentDefinition,
    CheckpointNotFoundError,
    EngineError,
    Event,
    ModelProviderConfig,
    ProviderKind,
    SessionExistsError,
    SessionNotFoundError,
    UnknownModelError,
)
from robinauts.agent_engines.contract.ports import AgentEngine, EngineSettings, StorageConfig
from robinauts_opencode_worker import checkpoints, config
from robinauts_opencode_worker.credentials import refused_text
from robinauts_opencode_worker.events import (
    Translation,
    done,
    error_text,
    final_message,
    refuses_credential,
)
from robinauts_opencode_worker.server import OpenCodeServer, ServerError
from robinauts_opencode_worker.settings import Settings, settings_from
from robinauts_worker_common.clone import GitError
from robinauts_worker_common.function_server import FunctionServer
from robinauts_worker_common.tasks import TaskPayloadError, code_task_starts
from robinauts_worker_common.worktrees import Repositories

_log = logging.getLogger(__name__)

ENGINE = "opencode"
"""The engine's name in a configuration: ``[agents.<id>] engine = "opencode"``."""

REPOSITORIES_FOLDER = "repos"
"""Where the clones of the flows' repositories are, under the workspaces: beside the sessions'
directories, whose names are ids and never this."""
SESSIONS_FOLDER = "sessions"
"""Under the engine's state: one file per conversation, the OpenCode sessions it made."""
CONNECTED = "server.connected"
CONNECT_SECONDS = 10.0
ABORT_SECONDS = 5.0
"""How long a cancelled or timed-out turn waits for OpenCode to stop before it is dropped."""

_END = object()


def init_opencode(settings: EngineSettings, storage: StorageConfig) -> AgentEngine:
    """The engine, with the worker's own settings read from the environment: the entry point
    the worker module calls. ``SettingsError`` for an environment that is not this worker's,
    which ``__main__`` has refused before the worker starts."""
    container = settings_from(os.environ)
    root = container.workspaces / REPOSITORIES_FOLDER
    return OpenCodeEngine(settings, container, Repositories.from_environ(root))


class OpenCodeEngine(AgentEngine):
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
        self._server: OpenCodeServer | None = None
        self._starting = asyncio.Lock()
        self._functions = FunctionServer()
        self._functions_started = False

    @property
    def repositories(self) -> Repositories | None:
        return self._repositories

    def kinds(self) -> frozenset[ProviderKind]:
        return frozenset(config.PACKAGES)

    def workspace(self, session_id: uuid.UUID) -> Path:
        """The conversation's directory: the project directory of its OpenCode sessions."""
        return self._container.workspaces / str(session_id)

    def sessions_file(self, session_id: uuid.UUID) -> Path:
        return self._container.state / SESSIONS_FOLDER / f"{session_id}.json"

    def sessions(self, session_id: uuid.UUID) -> list[str]:
        """The OpenCode sessions the conversation made, oldest first."""
        try:
            listed = json.loads(self.sessions_file(session_id).read_text())
        except (OSError, ValueError):
            return []
        return [str(session) for session in listed] if isinstance(listed, list) else []

    def _record(self, session_id: uuid.UUID, session: str) -> None:
        path = self.sessions_file(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        listed = self.sessions(session_id)
        if session not in listed:
            listed.append(session)
        written = path.with_suffix(".tmp")
        written.write_text(json.dumps(listed))
        written.replace(path)

    async def setup(self) -> None:
        self._container.workspaces.mkdir(parents=True, exist_ok=True)
        (self._container.state / SESSIONS_FOLDER).mkdir(parents=True, exist_ok=True)
        if not self._functions_started:
            await self._functions.start()
            self._functions_started = True
        try:
            await self.server()
        except EngineError as failed:
            # A turn starts it again, and fails with the reason if it still cannot.
            _log.error("%s", failed)

    async def server(self) -> OpenCodeServer:
        """The running server, started now if it is not."""
        async with self._starting:
            if self._server is not None and self._server.running:
                return self._server
            if self._server is not None:
                _log.warning("OpenCode's server stopped: it is started again")
                await self._server.stop()
                self._server = None
            configuration = config.configuration(
                self._settings.models,
                self._settings.keys,
                self._settings.tool_secrets,
                max_steps=self._container.max_steps,
            )
            try:
                self._server = await OpenCodeServer.start(
                    self._container.cli_path,
                    configuration,
                    os.environ,
                    self._container.workspaces,
                    self._container.start_seconds,
                )
            except ServerError as failed:
                raise EngineError(str(failed)) from None
            return self._server

    async def close(self) -> None:
        if self._server is not None:
            await self._server.stop()
            self._server = None
        if self._functions_started:
            await self._functions.stop()
            self._functions_started = False

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

    async def _call(
        self,
        server: OpenCodeServer,
        method: str,
        path: str,
        workspace: Path,
        body: Any = None,
    ) -> httpx.Response:
        try:
            return await server.client.request(
                method, path, params={"directory": str(workspace)}, json=body
            )
        except httpx.HTTPError as failed:
            raise EngineError(
                f"OpenCode's server did not answer {method} {path}: {failed}"
            ) from None

    async def _json(
        self,
        server: OpenCodeServer,
        method: str,
        path: str,
        workspace: Path,
        body: Any = None,
    ) -> Any:
        response = await self._call(server, method, path, workspace, body)
        if response.status_code >= 400:
            raise EngineError(f"OpenCode refused {method} {path}: {response.status_code}")
        return response.json()

    async def messages(
        self, server: OpenCodeServer, workspace: Path, session: str
    ) -> list[Mapping[str, Any]] | None:
        """The session's messages, oldest first; ``None`` for a session OpenCode does not hold."""
        response = await self._call(server, "GET", f"/session/{session}/message", workspace)
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise EngineError(f"OpenCode refused the messages of a session: {response.status_code}")
        listed = response.json()
        return listed if isinstance(listed, list) else []

    async def open_session(
        self,
        server: OpenCodeServer,
        session_id: uuid.UUID,
        workspace: Path,
        checkpoint_id: str | None,
    ) -> tuple[str, frozenset[str]]:
        """The OpenCode session the turn runs on, and the ids of the messages it holds before
        it: a new one for a first turn; the checkpoint's session when its message is the
        session's latest; a fork of that session up to the message otherwise. A session or a
        message OpenCode does not hold is ``CheckpointNotFoundError``."""
        if checkpoint_id is None:
            created = await self._json(
                server, "POST", "/session", workspace, {"title": str(session_id)}
            )
            session = str(created["id"])
            self._record(session_id, session)
            return session, frozenset()
        checkpoint = checkpoints.decode(checkpoint_id)
        if checkpoint.session not in self.sessions(session_id):
            raise CheckpointNotFoundError(
                f"session {checkpoint.session} of checkpoint {checkpoint_id!r} is not this"
                " conversation's"
            )
        messages = await self.messages(server, workspace, checkpoint.session)
        if messages is None:
            raise CheckpointNotFoundError(
                f"OpenCode holds no session {checkpoint.session} of checkpoint {checkpoint_id!r}"
            )
        ids = [str((message.get("info") or {}).get("id")) for message in messages]
        if checkpoint.message not in ids:
            raise CheckpointNotFoundError(
                f"session {checkpoint.session} holds no message {checkpoint.message} of"
                f" checkpoint {checkpoint_id!r}"
            )
        index = ids.index(checkpoint.message)
        if index == len(ids) - 1:
            return checkpoint.session, frozenset(ids)
        # A fork copies the messages before the one it is given: the one after the answer.
        forked = await self._json(
            server,
            "POST",
            f"/session/{checkpoint.session}/fork",
            workspace,
            {"messageID": ids[index + 1]},
        )
        session = str(forked["id"])
        self._record(session_id, session)
        copied = await self.messages(server, workspace, session) or []
        return session, frozenset(str((m.get("info") or {}).get("id")) for m in copied)

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
        server = await self.server()
        queue: asyncio.Queue[object] = asyncio.Queue()
        connected = asyncio.Event()
        listener = asyncio.create_task(self._listen(server, workspace, queue, connected))
        token = self._functions.bind(agent.functions) if agent.functions else None
        session: str | None = None
        translation: Translation | None = None
        try:
            async with asyncio.timeout_at(deadline):
                # The stream is opened before the prompt: it replays nothing it missed.
                try:
                    await asyncio.wait_for(connected.wait(), CONNECT_SECONDS)
                except TimeoutError:
                    raise EngineError("OpenCode's event stream did not open") from None
                session, before = await self.open_session(
                    server, session_id, workspace, checkpoint_id
                )
                await self._prepare(server, workspace, session, agent, token)
                translation = Translation(
                    session,
                    before,
                    name=lambda tool: config.tool_name(tool, self._settings.models),
                )
                await self._prompt(
                    server, workspace, session, agent, prompt, provider, model_config.name
                )
            while not translation.ended:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise TimeoutError
                try:
                    item = await asyncio.wait_for(queue.get(), remaining)
                except TimeoutError:
                    raise TimeoutError from None
                if item is _END:
                    raise EngineError("OpenCode's event stream ended before the turn did")
                if isinstance(item, BaseException):
                    raise EngineError(f"OpenCode's event stream failed: {item}")
                kind, properties = item  # type: ignore[misc]
                for event in translation.translate(kind, properties):
                    yield event
            async with asyncio.timeout_at(deadline):
                messages = await self.messages(server, workspace, session) or []
            final = final_message(messages, translation.user_message, translation.before)
            error = translation.error
            if final is not None and (final.get("info") or {}).get("error"):
                error = final["info"]["error"]
            if error or final is None:
                raise EngineError(self._failure(error, provider))
            message = str(final["info"]["id"])
            checkpoint = checkpoints.encode(checkpoints.Checkpoint(session, message))
            yield done(final, translation.streamed(), checkpoint)
        finally:
            if session is not None and (translation is None or not translation.ended):
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    await asyncio.wait_for(
                        asyncio.shield(
                            self._call(server, "POST", f"/session/{session}/abort", workspace)
                        ),
                        ABORT_SECONDS,
                    )
            if token is not None:
                self._functions.unbind(token)
            listener.cancel()
            with contextlib.suppress(BaseException):
                await asyncio.shield(listener)

    def _failure(self, error: Mapping[str, Any] | None, provider: ModelProviderConfig) -> str:
        """What the operator reads on a turn that did not end well. The log says that it
        happened, and why when the key was refused, never what was said."""
        if refuses_credential(error):
            text = refused_text(provider)
            _log.error(text)
            return text
        name = (error or {}).get("name", "no answer")
        _log.warning("OpenCode ended a turn badly: %s", name)
        return error_text(error) or "OpenCode ended the turn without an answer"

    async def _listen(
        self,
        server: OpenCodeServer,
        workspace: Path,
        queue: asyncio.Queue[object],
        connected: asyncio.Event,
    ) -> None:
        try:
            async for kind, properties in server.events(workspace):
                if kind == CONNECTED:
                    connected.set()
                    continue
                queue.put_nowait((kind, properties))
        except asyncio.CancelledError:
            raise
        except Exception as failed:
            queue.put_nowait(failed)
        finally:
            queue.put_nowait(_END)

    async def _prepare(
        self,
        server: OpenCodeServer,
        workspace: Path,
        session: str,
        agent: AgentDefinition,
        token: str | None,
    ) -> None:
        """The session's rules for this turn's agent, and the tools run here, under the turn's
        token, as an MCP server of the turn's directory."""
        rules = config.tool_rules(agent.tools, self._settings.models, token is not None)
        response = await self._call(
            server, "PATCH", f"/session/{session}", workspace, {"permission": rules}
        )
        if response.status_code >= 400:
            raise EngineError(f"OpenCode refused the session's rules: {response.status_code}")
        if token is None:
            return
        added = await self._call(
            server,
            "POST",
            "/mcp",
            workspace,
            {
                "name": config.FUNCTIONS_SERVER,
                "config": {
                    "type": "remote",
                    "url": self._functions.url,
                    "headers": {"Authorization": f"Bearer {token}"},
                    "enabled": True,
                    "oauth": False,
                },
            },
        )
        if added.status_code >= 400:
            raise EngineError(f"OpenCode refused the worker's tools: {added.status_code}")

    async def _prompt(
        self,
        server: OpenCodeServer,
        workspace: Path,
        session: str,
        agent: AgentDefinition,
        prompt: str,
        provider: ModelProviderConfig,
        model_name: str,
    ) -> None:
        body: dict[str, Any] = {
            "parts": [{"type": "text", "text": prompt}],
            "model": {"providerID": config.provider_id(provider), "modelID": model_name},
            "agent": config.AGENT,
        }
        if agent.system_prompt:
            body["system"] = agent.system_prompt
        response = await self._call(
            server, "POST", f"/session/{session}/prompt_async", workspace, body
        )
        if response.status_code >= 400:
            # Why is said on the event stream alone, as a session error.
            raise EngineError(f"OpenCode refused the prompt: {response.status_code}")

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
        sessions = self.sessions(session_id)
        if sessions:
            try:
                server = await self.server()
                for session in reversed(sessions):
                    response = await self._call(server, "DELETE", f"/session/{session}", workspace)
                    if response.status_code >= 400 and response.status_code != 404:
                        _log.warning("OpenCode session %s was not deleted", session)
            except EngineError as failed:
                _log.warning("the OpenCode sessions of %s were not deleted: %s", session_id, failed)
        self.sessions_file(session_id).unlink(missing_ok=True)
        shutil.rmtree(workspace, ignore_errors=True)
