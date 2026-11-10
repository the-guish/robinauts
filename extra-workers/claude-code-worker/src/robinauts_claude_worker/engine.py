# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The engine: a turn is one query of the Claude Agent SDK, which drives the Claude Code binary;
a conversation is one Claude Code session, under a working directory of its own.

The state is the SDK's transcripts, under ``CLAUDE_CONFIG_DIR`` (``~/.claude`` when unset), and
nothing else: a checkpoint names the session and the message the turn ended on, so continuing
is a resume, and continuing from an earlier answer, as an edit or a regenerate does, is a fork
of the session at that message. A conversation's first turn starts a new session. A turn that
failed or was cancelled leaves what it wrote in the transcript; the next turn resumes at the
checkpoint before it, which cuts it off. ``forget`` deletes the directory and the transcripts.

``init_claude_code`` is the engine's factory, registered under the ``robinauts.engines``
entry-point group as ``claude-code``: the worker module builds the engine with it, as it builds
the engines the backend ships. The storage it is handed is not used: the transcripts are the
state, and the worker is handed no database.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import uuid
from collections.abc import AsyncGenerator, Mapping
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultError,
    get_session_messages,
    project_key_for_directory,
)

from robinauts.agent_engines.contract.domain import (
    AgentDefinition,
    CheckpointNotFoundError,
    EngineError,
    Event,
    ProviderKind,
    SessionExistsError,
    SessionNotFoundError,
    UnknownModelError,
)
from robinauts.agent_engines.contract.ports import (
    AgentEngine,
    EngineSettings,
    StorageConfig,
)
from robinauts_claude_worker import checkpoints, tools
from robinauts_claude_worker.credentials import refused_text, subprocess_environment
from robinauts_claude_worker.events import Translation
from robinauts_claude_worker.settings import (
    AuthMode,
    Settings,
    SystemPromptMode,
    settings_from,
)
from robinauts_worker_common.clone import GitError
from robinauts_worker_common.tasks import TaskPayloadError, code_task_starts
from robinauts_worker_common.worktrees import Repositories

_log = logging.getLogger(__name__)

ENGINE = "claude-code"
"""The engine's name in a configuration: ``[agents.<id>] engine = "claude-code"``."""

CONFIG_DIR_VARIABLE = "CLAUDE_CONFIG_DIR"
REPOSITORIES_FOLDER = "repos"
"""Where the clones of the flows' repositories are, under the workspaces: beside the sessions'
directories, whose names are ids and never this."""
INTERRUPT_SECONDS = 5.0
"""How long a cancelled or timed-out turn waits for Claude Code to stop before it is dropped."""

_END = object()


def config_dir(environ: dict[str, str] | os._Environ[str] = os.environ) -> Path:
    """Where Claude Code keeps its transcripts, under ``projects/``."""
    return Path(environ.get(CONFIG_DIR_VARIABLE) or "~/.claude").expanduser()


def init_claude_code(settings: EngineSettings, storage: StorageConfig) -> AgentEngine:
    """The engine, with the worker's own settings read from the environment: the entry point
    the worker module calls. ``SettingsError`` for an environment that is not this worker's,
    which ``__main__`` has refused before the worker starts. The repositories of the flows'
    settings, when ``ROBINAUTS_FLOWS_DIR`` names them, are cloned under the workspaces."""
    container = settings_from(os.environ)
    root = container.workspaces / REPOSITORIES_FOLDER
    return ClaudeCodeEngine(settings, container, Repositories.from_environ(root))


class ClaudeCodeEngine(AgentEngine):
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
            return frozenset({ProviderKind.ANTHROPIC})
        return frozenset({ProviderKind.ANTHROPIC, ProviderKind.ANTHROPIC_COMPATIBLE})

    def workspace(self, session_id: uuid.UUID) -> Path:
        """The conversation's directory: Claude Code's ``cwd``, and what its transcripts are
        filed under."""
        return self._container.workspaces / str(session_id)

    async def setup(self) -> None:
        self._container.workspaces.mkdir(parents=True, exist_ok=True)

    async def create(self, session_id: uuid.UUID) -> None:
        workspace = self.workspace(session_id)
        if workspace.exists():
            raise SessionExistsError(f"session {session_id} exists")
        workspace.mkdir(parents=True)

    async def exists(self, session_id: uuid.UUID) -> bool:
        return self.workspace(session_id).is_dir()

    def plan(self, workspace: Path, checkpoint_id: str | None) -> dict[str, Any]:
        """How the SDK is to open the session: a new one for a first turn; a resume of the
        checkpoint's session when its message is the session's latest; a fork of the session
        at that message otherwise. A session the transcripts do not hold is
        ``CheckpointNotFoundError``; a message they do not hold is resumed at the session's
        end, and said so."""
        if checkpoint_id is None:
            return {"session_id": str(uuid.uuid4())}
        checkpoint = checkpoints.decode(checkpoint_id)
        try:
            messages = get_session_messages(checkpoint.session, directory=str(workspace))
        except (OSError, ValueError) as error:
            raise CheckpointNotFoundError(
                f"session {checkpoint.session} of checkpoint {checkpoint_id!r}: {error}"
            ) from None
        if not messages:
            raise CheckpointNotFoundError(
                f"session {checkpoint.session} of checkpoint {checkpoint_id!r} has no transcript"
            )
        answers = [m.uuid for m in messages if m.type == "assistant" and not m.parent_tool_use_id]
        if checkpoint.message not in {m.uuid for m in messages}:
            _log.warning(
                "checkpoint %s names a message its session does not hold: resumed at the end",
                checkpoint_id,
            )
            return {"resume": checkpoint.session}
        if answers and answers[-1] == checkpoint.message:
            return {"resume": checkpoint.session}
        return {
            "resume": checkpoint.session,
            "resume_session_at": checkpoint.message,
            "fork_session": True,
        }

    def options(
        self,
        workspace: Path,
        agent: AgentDefinition,
        model_name: str,
        environment: dict[str, str],
        opening: dict[str, Any],
    ) -> ClaudeAgentOptions:
        container = self._container
        system_prompt: Any = agent.system_prompt
        if container.system_prompt is SystemPromptMode.PRESET:
            system_prompt = {
                "type": "preset",
                "preset": "claude_code",
                "append": agent.system_prompt,
            }
        servers = tools.mcp_servers(agent, self._settings.models, self._settings.tool_secrets)
        if (sdk := tools.sdk_server(agent)) is not None:
            servers[tools.SDK_SERVER] = sdk
        return ClaudeAgentOptions(
            cwd=workspace,
            model=model_name,
            system_prompt=system_prompt,
            permission_mode=container.permission_mode,  # type: ignore[arg-type]
            allowed_tools=[*container.allowed_tools, *tools.allowed(agent)],
            disallowed_tools=[
                *container.disallowed_tools,
                *tools.excluded(agent, self._settings.models),
            ],
            mcp_servers=servers,
            setting_sources=list(container.setting_sources),  # type: ignore[arg-type]
            env=environment,
            max_turns=container.max_turns,
            max_budget_usd=container.max_budget_usd,
            effort=container.effort,  # type: ignore[arg-type]
            include_partial_messages=container.partial_messages,
            cli_path=container.cli_path,
            stderr=lambda line: _log.debug("claude: %s", line.rstrip()),
            **opening,
        )

    def task_starts(self, task_payload: Mapping[str, Any] | None) -> dict[str, str] | None:
        """The repositories a ``code_task/1`` payload starts the session's worktrees of, each
        with its branch; ``None`` for any other payload, or none. ``EngineError`` for a code
        task this worker cannot set up."""
        known = () if self._repositories is None else self._repositories.repositories
        try:
            return code_task_starts(task_payload, known)
        except TaskPayloadError as refused:
            raise EngineError(str(refused)) from None

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
        environment = subprocess_environment(self._container.auth, provider, self._settings.keys)
        opening = self.plan(workspace, checkpoint_id)
        options = self.options(workspace, agent, model_config.name, environment, opening)
        translation = Translation(partial=self._container.partial_messages)
        # The SDK runs in a task of its own, so that a cancellation or the deadline reaches it
        # as one interrupt, however the consumer of these events is placed.
        queue: asyncio.Queue[object] = asyncio.Queue()
        pump = asyncio.create_task(self._pump(options, prompt, translation, queue))
        try:
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise TimeoutError
                try:
                    item = await asyncio.wait_for(queue.get(), remaining)
                except TimeoutError:
                    raise TimeoutError from None
                if item is _END:
                    break
                if isinstance(item, BaseException):
                    raise item
                yield item  # type: ignore[misc]
            failure = translation.failure()
            if failure is not None:
                # The turn's error is the operator's to read on the turn; the log says that
                # it happened, and why when the credential was refused, never what was said.
                if translation.credential_refused():
                    failure = refused_text(self._container.auth, provider)
                    _log.error(failure)
                else:
                    result = translation.result
                    _log.warning(
                        "Claude Code ended a turn badly: %s",
                        "no result" if result is None else result.subtype,
                    )
                raise EngineError(failure)
            result = translation.result
            assert result is not None
            if translation.last_assistant_uuid is None:
                raise EngineError("Claude Code answered without an assistant message")
            checkpoint = checkpoints.Checkpoint(result.session_id, translation.last_assistant_uuid)
            yield translation.done(checkpoints.encode(checkpoint))
        finally:
            if not pump.done():
                pump.cancel()
            with contextlib.suppress(BaseException):
                await asyncio.shield(pump)

    async def _pump(
        self,
        options: ClaudeAgentOptions,
        prompt: str,
        translation: Translation,
        queue: asyncio.Queue[object],
    ) -> None:
        """Run the query and put its events on the queue, then ``_END``; an error instead of the
        end. Cancelled, it interrupts Claude Code, bounded, before it lets go."""
        try:
            async with ClaudeSDKClient(options) as client:
                try:
                    await client.query(prompt)
                    async for message in client.receive_response():
                        for event in translation.translate(message):
                            await queue.put(event)
                except asyncio.CancelledError:
                    with contextlib.suppress(Exception, asyncio.CancelledError):
                        await asyncio.wait_for(
                            asyncio.shield(client.interrupt()), INTERRUPT_SECONDS
                        )
                    raise
        except asyncio.CancelledError:
            raise
        except ResultError as ended:
            # The SDK's own word for a turn that ended on an error result.
            if translation.result is None:
                queue.put_nowait(EngineError(str(ended)))
        except Exception as error:
            queue.put_nowait(error)
        finally:
            queue.put_nowait(_END)

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
        transcripts = config_dir() / "projects" / project_key_for_directory(str(workspace))
        shutil.rmtree(transcripts, ignore_errors=True)
        shutil.rmtree(workspace, ignore_errors=True)
