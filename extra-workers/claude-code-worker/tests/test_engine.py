# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The engine without a Claude Code process: how it opens a session for a checkpoint, what it
refuses before it runs anything, and what it forgets."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import replace
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import SessionMessage

from robinauts.agent_engines.contract.domain import (
    AgentDefinition,
    CheckpointNotFoundError,
    EngineError,
    ModelConfig,
    ModelProviderConfig,
    ModelsConfig,
    ProviderKind,
    SessionExistsError,
    SessionNotFoundError,
    UnknownModelError,
)
from robinauts.agent_engines.contract.ports import (
    ENTRY_POINTS,
    EngineSettings,
    ProviderKeyLookup,
    StorageConfig,
    StorageKind,
    ToolSecretLookup,
    installed,
)
from robinauts_claude_worker import engine as engine_module
from robinauts_claude_worker.checkpoints import Checkpoint, encode
from robinauts_claude_worker.engine import ENGINE, ClaudeCodeEngine, init_claude_code
from robinauts_claude_worker.settings import Settings, SettingsError, settings_from
from robinauts_worker_common.worktrees import Repositories, Repository, WorktreeError

SESSION = str(uuid.uuid4())
A1, A2, U1 = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())


class Keys(ProviderKeyLookup):
    def key_for(self, provider_id: str) -> str:
        return "k"


class Secrets(ToolSecretLookup):
    def secret_for(self, server_id: str) -> str:
        return "s"


SETTINGS = EngineSettings(
    models=ModelsConfig(
        providers={
            "anthropic": ModelProviderConfig(
                "anthropic", ProviderKind.ANTHROPIC, "ANTHROPIC_API_KEY"
            ),
            "openai": ModelProviderConfig("openai", ProviderKind.OPENAI, "OPENAI_API_KEY"),
        },
        models={
            "opus": ModelConfig("opus", "anthropic", "claude-opus-5-5"),
            "gpt": ModelConfig("gpt", "openai", "gpt-x"),
        },
    ),
    keys=Keys(),
    tool_secrets=Secrets(),
)
NOTHING = StorageConfig(StorageKind.IN_PROCESS, {})


def container(tmp_path: Path, auth: str = "personal") -> Settings:
    return settings_from(
        {"ROBINAUTS_CLAUDE_AUTH": auth, "ROBINAUTS_CLAUDE_WORKSPACES": str(tmp_path / "workspaces")}
    )


def transcript(*entries: tuple[str, str]) -> list[SessionMessage]:
    return [
        SessionMessage(type=kind, uuid=id_, session_id=SESSION, message={}) for kind, id_ in entries
    ]


def test_the_entry_point_builds_the_engine_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What the worker module does: find the engine by its name, call it with the engine
    settings and a storage it does not use, and get an engine on this worker's settings."""
    (found,) = entry_points(group=ENTRY_POINTS, name=ENGINE)
    assert found.load() is init_claude_code
    assert installed()[ENGINE] is init_claude_code
    monkeypatch.setenv("ROBINAUTS_CLAUDE_AUTH", "deployed")
    monkeypatch.setenv("ROBINAUTS_CLAUDE_WORKSPACES", str(tmp_path))
    engine = init_claude_code(SETTINGS, NOTHING)
    assert isinstance(engine, ClaudeCodeEngine)
    assert engine.kinds() == {ProviderKind.ANTHROPIC, ProviderKind.ANTHROPIC_COMPATIBLE}
    assert engine.workspace(uuid.UUID(SESSION)) == tmp_path / SESSION
    monkeypatch.delenv("ROBINAUTS_CLAUDE_AUTH")
    with pytest.raises(SettingsError, match="ROBINAUTS_CLAUDE_AUTH"):
        init_claude_code(SETTINGS, NOTHING)


def test_the_kinds_each_mode_reaches(tmp_path: Path) -> None:
    assert ClaudeCodeEngine(SETTINGS, container(tmp_path)).kinds() == {ProviderKind.ANTHROPIC}
    deployed = ClaudeCodeEngine(SETTINGS, container(tmp_path, "deployed"))
    assert deployed.kinds() == {ProviderKind.ANTHROPIC, ProviderKind.ANTHROPIC_COMPATIBLE}
    assert ENGINE == "claude-code"


def test_a_session_is_a_directory_of_its_own(tmp_path: Path) -> None:
    engine = ClaudeCodeEngine(SETTINGS, container(tmp_path))
    session = uuid.uuid4()
    asyncio.run(engine.setup())
    assert not asyncio.run(engine.exists(session))
    asyncio.run(engine.create(session))
    assert asyncio.run(engine.exists(session))
    assert engine.workspace(session) == tmp_path / "workspaces" / str(session)
    with pytest.raises(SessionExistsError):
        asyncio.run(engine.create(session))
    asyncio.run(engine.forget(session))
    assert not asyncio.run(engine.exists(session))
    asyncio.run(engine.forget(session))


def test_a_first_turn_opens_a_new_session(tmp_path: Path) -> None:
    engine = ClaudeCodeEngine(SETTINGS, container(tmp_path))
    opening = engine.plan(tmp_path, None)
    assert list(opening) == ["session_id"]
    uuid.UUID(opening["session_id"])


def test_the_latest_answer_is_resumed_and_an_earlier_one_forked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = ClaudeCodeEngine(SETTINGS, container(tmp_path))
    asked = []

    def messages(session_id: str, directory: str | None = None, **_: Any) -> list[SessionMessage]:
        asked.append((session_id, directory))
        return transcript(("user", U1), ("assistant", A1), ("user", U1), ("assistant", A2))

    monkeypatch.setattr(engine_module, "get_session_messages", messages)
    latest = engine.plan(tmp_path, encode(Checkpoint(SESSION, A2)))
    assert latest == {"resume": SESSION}
    earlier = engine.plan(tmp_path, encode(Checkpoint(SESSION, A1)))
    assert earlier == {"resume": SESSION, "resume_session_at": A1, "fork_session": True}
    unknown = engine.plan(tmp_path, encode(Checkpoint(SESSION, str(uuid.uuid4()))))
    assert unknown == {"resume": SESSION}
    assert asked == [(SESSION, str(tmp_path))] * 3


def test_a_session_the_transcripts_do_not_hold_is_no_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = ClaudeCodeEngine(SETTINGS, container(tmp_path))
    monkeypatch.setattr(engine_module, "get_session_messages", lambda *a, **k: [])
    with pytest.raises(CheckpointNotFoundError, match="has no transcript"):
        engine.plan(tmp_path, encode(Checkpoint(SESSION, A1)))

    def missing(*a: Any, **k: Any) -> list[SessionMessage]:
        raise FileNotFoundError("no such file")

    monkeypatch.setattr(engine_module, "get_session_messages", missing)
    with pytest.raises(CheckpointNotFoundError, match="no such file"):
        engine.plan(tmp_path, encode(Checkpoint(SESSION, A1)))
    with pytest.raises(CheckpointNotFoundError, match="not one of this engine's"):
        engine.plan(tmp_path, "something-else")


async def first(stream: Any) -> Any:
    async for event in stream:
        return event
    return None


def test_what_a_turn_refuses_before_claude_code_runs(tmp_path: Path) -> None:
    engine = ClaudeCodeEngine(SETTINGS, container(tmp_path))
    session = uuid.uuid4()
    agent = AgentDefinition("be brief")
    turn = dict(model="opus", checkpoint_id=None, timeout_seconds=10.0)
    with pytest.raises(SessionNotFoundError):
        asyncio.run(first(engine.stream(session, agent, "hi", **turn)))
    asyncio.run(engine.create(session))
    with pytest.raises(UnknownModelError):
        asyncio.run(first(engine.stream(session, agent, "hi", **{**turn, "model": "nope"})))
    with pytest.raises(Exception, match="kind this mode cannot reach"):
        asyncio.run(first(engine.stream(session, agent, "hi", **{**turn, "model": "gpt"})))


class Recorded(Repositories):
    """The sessions' worktrees, recorded rather than made; a failure, or a wait, when told."""

    def __init__(self, root: Path, *, fail: bool = False, wait: float = 0.0) -> None:
        super().__init__(root, [Repository("hello", "https://github.com/octo/hello.git")])
        self.calls: list[tuple[str, str, Path]] = []
        self._fail = fail
        self._wait = wait

    async def add_worktrees(self, session: str, folder: Path) -> list[Path]:
        self.calls.append(("add", session, folder))
        await asyncio.sleep(self._wait)
        if self._fail:
            raise WorktreeError("the worktree of hello failed:\nfatal: no")
        return [folder / "hello"]

    async def remove_worktrees(self, session: str, folder: Path) -> None:
        self.calls.append(("remove", session, folder))
        if self._fail:
            raise WorktreeError("removing the worktree of hello failed")


def test_the_worktrees_are_made_before_the_agent_starts(tmp_path: Path) -> None:
    session = uuid.uuid4()
    agent = AgentDefinition("be brief")
    turn = dict(model="opus", checkpoint_id=None, timeout_seconds=10.0)
    # A failure ends the turn, as the engine's error, before Claude Code runs.
    failing = Recorded(tmp_path / "repos", fail=True)
    engine = ClaudeCodeEngine(SETTINGS, container(tmp_path), failing)
    asyncio.run(engine.create(session))
    with pytest.raises(EngineError, match="the worktree of hello failed"):
        asyncio.run(first(engine.stream(session, agent, "hi", **turn)))
    assert failing.calls == [("add", str(session), engine.workspace(session))]
    # The turn's deadline holds them too.
    slow = ClaudeCodeEngine(SETTINGS, container(tmp_path), Recorded(tmp_path, wait=5.0))
    with pytest.raises(TimeoutError):
        asyncio.run(first(slow.stream(session, agent, "hi", **{**turn, "timeout_seconds": 0.1})))
    # A refusal comes first: no worktree for a turn that cannot run.
    failing.calls.clear()
    with pytest.raises(UnknownModelError):
        asyncio.run(first(engine.stream(session, agent, "hi", **{**turn, "model": "nope"})))
    assert failing.calls == []


def test_forgetting_a_session_removes_its_worktrees(tmp_path: Path) -> None:
    session = uuid.uuid4()
    for fail in (False, True):
        recorded = Recorded(tmp_path / "repos", fail=fail)
        engine = ClaudeCodeEngine(SETTINGS, container(tmp_path), recorded)
        asyncio.run(engine.create(session))
        asyncio.run(engine.forget(session))
        assert recorded.calls == [("remove", str(session), engine.workspace(session))]
        assert not engine.workspace(session).exists()


def test_the_repositories_are_cloned_under_the_workspaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ROBINAUTS_CLAUDE_AUTH", "deployed")
    monkeypatch.setenv("ROBINAUTS_CLAUDE_WORKSPACES", str(tmp_path / "workspaces"))
    monkeypatch.delenv("ROBINAUTS_FLOWS_DIR", raising=False)
    assert init_claude_code(SETTINGS, NOTHING).repositories is None
    flows = tmp_path / "flows"
    flows.mkdir()
    (flows / "flows.toml").write_text(
        '[repositories.hello]\nurl = "https://github.com/octo/hello"\nbranches = ["main"]\n'
    )
    monkeypatch.setenv("ROBINAUTS_FLOWS_DIR", str(flows))
    repositories = init_claude_code(SETTINGS, NOTHING).repositories
    assert repositories is not None
    assert repositories.root == tmp_path / "workspaces" / "repos"
    assert repositories.repositories == (Repository("hello", "https://github.com/octo/hello.git"),)


def test_the_options_claude_code_is_started_with(tmp_path: Path) -> None:
    deployed = container(tmp_path, "deployed")
    engine = ClaudeCodeEngine(SETTINGS, deployed)
    agent = AgentDefinition("be brief", tools=(), functions=())
    options = engine.options(
        tmp_path, agent, "claude-opus-5-5", {"ANTHROPIC_API_KEY": "k"}, {"session_id": SESSION}
    )
    assert options.cwd == tmp_path
    assert options.model == "claude-opus-5-5"
    assert options.system_prompt == {
        "type": "preset",
        "preset": "claude_code",
        "append": "be brief",
    }
    assert options.permission_mode == "dontAsk"
    assert "Bash" in options.allowed_tools
    assert options.disallowed_tools == ["AskUserQuestion", "ExitPlanMode"]
    assert options.setting_sources == []
    assert options.env == {"ANTHROPIC_API_KEY": "k"}
    assert options.session_id == SESSION
    assert options.include_partial_messages is True
    custom = ClaudeCodeEngine(SETTINGS, replace(deployed, system_prompt="custom"))
    assert custom.options(tmp_path, agent, "m", {}, {}).system_prompt == "be brief"
