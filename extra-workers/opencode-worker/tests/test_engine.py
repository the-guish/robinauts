# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The engine against a stand-in of ``opencode serve`` (stand_in_opencode.py), run as a process
of its own the way OpenCode is: one conversation's turns, continued, regenerated, failed,
cancelled and forgotten; the tools run in this process, called over the worker's own MCP
server; and what the engine refuses before it runs anything."""

from __future__ import annotations

import asyncio
import json
import stat
import sys
import uuid
from collections.abc import Mapping
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

import pytest

from robinauts.agent_engines.contract.domain import (
    AgentDefinition,
    CheckpointNotFoundError,
    Done,
    EngineError,
    Event,
    FunctionTool,
    FunctionToolError,
    ModelConfig,
    ModelProviderConfig,
    ModelsConfig,
    ProviderKind,
    ReasoningDelta,
    SessionExistsError,
    SessionNotFoundError,
    TextDelta,
    ToolCall,
    ToolResult,
    ToolServerConfig,
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
from robinauts_opencode_worker.engine import ENGINE, OpenCodeEngine, init_opencode
from robinauts_opencode_worker.settings import Settings, settings_from

STAND_IN = Path(__file__).with_name("stand_in_opencode.py")


class Keys(ProviderKeyLookup):
    def key_for(self, provider_id: str) -> str:
        return "the-key"


class Secrets(ToolSecretLookup):
    def secret_for(self, server_id: str) -> str:
        return "the-secret"


SETTINGS = EngineSettings(
    models=ModelsConfig(
        providers={
            "claude": ModelProviderConfig("claude", ProviderKind.ANTHROPIC, "ANTHROPIC_API_KEY")
        },
        models={"opus": ModelConfig("opus", "claude", "claude-opus-5-5")},
        tool_servers={"kb": ToolServerConfig("kb", "https://kb/mcp", "KB")},
    ),
    keys=Keys(),
    tool_secrets=Secrets(),
)
NOTHING = StorageConfig(StorageKind.IN_PROCESS, {})


async def _find(arguments: Mapping[str, Any]) -> str:
    if arguments.get("fail"):
        raise FunctionToolError("asked to fail")
    return f"found {arguments.get('q')}"


FIND = FunctionTool("find", "Find it", {"type": "object"}, _find)
AGENT = AgentDefinition("You are a robinaut.", tools=("kb",), functions=(FIND,))


def container(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    cli = tmp_path / "opencode"
    cli.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{STAND_IN}" "$@"\n')
    cli.chmod(cli.stat().st_mode | stat.S_IXUSR)
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    # The server inherits the worker's environment, as OpenCode does.
    monkeypatch.setenv("XDG_DATA_HOME", str(data))
    return settings_from(
        {
            "ROBINAUTS_OPENCODE_WORKSPACES": str(tmp_path / "workspaces"),
            "ROBINAUTS_OPENCODE_STATE": str(tmp_path / "state"),
            "ROBINAUTS_OPENCODE_CLI_PATH": str(cli),
        }
    )


async def turn(
    engine: OpenCodeEngine,
    session: uuid.UUID,
    prompt: str,
    checkpoint: str | None,
    *,
    timeout: float = 30.0,
    model: str = "opus",
) -> list[Event]:
    return [
        event
        async for event in engine.stream(
            session, AGENT, prompt, model=model, checkpoint_id=checkpoint, timeout_seconds=timeout
        )
    ]


def answer(events: list[Event]) -> Done:
    done = events[-1]
    assert isinstance(done, Done)
    assert "".join(e.text for e in events if isinstance(e, TextDelta)) == done.text
    return done


def log(tmp_path: Path) -> list[dict[str, Any]]:
    path = tmp_path / "data" / "stand-in.log.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_the_entry_point_builds_the_engine_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (found,) = entry_points(group=ENTRY_POINTS, name=ENGINE)
    assert found.load() is init_opencode
    assert installed()[ENGINE] is init_opencode
    monkeypatch.setenv("ROBINAUTS_OPENCODE_WORKSPACES", str(tmp_path))
    engine = init_opencode(SETTINGS, NOTHING)
    assert isinstance(engine, OpenCodeEngine)
    assert engine.kinds() == set(ProviderKind)


def test_a_conversation_on_opencode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """One conversation the way the platform holds it: a first turn, a second that continues
    it, a regenerate of the first answer, tools, a failed turn and one after it, a cancelled
    turn, and the conversation forgotten."""

    async def journey() -> None:
        engine = OpenCodeEngine(SETTINGS, container(tmp_path, monkeypatch))
        await engine.setup()
        try:
            session = uuid.uuid4()
            await engine.create(session)
            with pytest.raises(SessionExistsError):
                await engine.create(session)

            first = await turn(engine, session, "hello", None)
            assert first[0] == ReasoningDelta("pondering")
            one = answer(first)
            assert one.text == "You said hello; before: []"

            two = answer(await turn(engine, session, "again", one.checkpoint_id))
            assert two.text == "You said again; before: ['hello']"
            assert two.checkpoint_id.split(":")[0] == one.checkpoint_id.split(":")[0]

            # A regenerate of the second answer: a fork that keeps the first turn alone.
            regenerated = answer(await turn(engine, session, "instead", one.checkpoint_id))
            assert regenerated.text == "You said instead; before: ['hello']"
            assert regenerated.checkpoint_id.split(":")[0] != one.checkpoint_id.split(":")[0]
            three = answer(await turn(engine, session, "more", two.checkpoint_id))
            assert three.text == "You said more; before: ['hello', 'again']"

            # A tool run here, called by OpenCode over the worker's own MCP server.
            tooled = await turn(engine, session, 'tool find {"q": "it"}', three.checkpoint_id)
            assert tooled[:2] == [
                ToolCall("call-1", "find", {"q": "it"}),
                ToolResult("call-1", "find", "found it", False),
            ]
            assert answer(tooled).text == "found it"
            failed = await turn(engine, session, 'tool find {"fail": true}', three.checkpoint_id)
            assert failed[1] == ToolResult("call-1", "find", "asked to fail", True)

            bash = await turn(engine, session, "bash", three.checkpoint_id)
            assert bash[:2] == [
                ToolCall("call-2", "bash", {"command": "ls"}),
                ToolResult("call-2", "bash", "a\n", False),
            ]

            # A refused key: the turn fails, saying which variable to fix, and never the key.
            with pytest.raises(EngineError, match="ANTHROPIC_API_KEY") as refused:
                await turn(engine, session, "unauthorized", three.checkpoint_id)
            assert "the-key" not in str(refused.value)
            after = answer(await turn(engine, session, "after", three.checkpoint_id))
            assert after.text == "You said after; before: ['hello', 'again', 'more']"

            # A turn past its deadline is aborted, and ends by raising TimeoutError.
            with pytest.raises(TimeoutError):
                await turn(engine, session, "sleep", after.checkpoint_id, timeout=2.0)
            requests = log(tmp_path)
            assert sum(1 for r in requests if r.get("path", "").endswith("/abort")) == 1

            # What the server was started with: the keys in its environment, nothing that asks.
            (started,) = [r for r in requests if "config" in r]
            assert started["client"] == "sdk"
            assert "OPENCODE_DISABLE_PROJECT_CONFIG" in started["switches"]
            assert started["config"]["provider"]["robinauts-claude"]["options"]["apiKey"] == (
                "the-key"
            )
            prompts = [r["body"] for r in requests if r.get("path", "").endswith("/prompt_async")]
            assert {p["system"] for p in prompts} == {"You are a robinaut."}
            assert {p["model"]["providerID"] for p in prompts} == {"robinauts-claude"}
            # Each turn's rules: the agent's server, and the tools run here.
            rules = [r["body"]["permission"] for r in requests if r.get("method") == "PATCH"]
            assert {(rule["permission"], rule["action"]) for rule in rules[0]} == {
                ("kb_*", "allow"),
                ("robinauts_*", "allow"),
            }

            sessions = engine.sessions(session)
            assert len(sessions) >= 3
            await engine.forget(session)
            assert not await engine.exists(session)
            deleted = [r["path"] for r in log(tmp_path) if r.get("method") == "DELETE"]
            assert deleted == [f"/session/{s}" for s in reversed(sessions)]
            await engine.forget(session)
        finally:
            await engine.close()

    asyncio.run(journey())


def test_what_the_engine_refuses_before_it_runs_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def refusals() -> None:
        engine = OpenCodeEngine(SETTINGS, container(tmp_path, monkeypatch))
        await engine.setup()
        try:
            session, other = uuid.uuid4(), uuid.uuid4()
            with pytest.raises(SessionNotFoundError):
                await turn(engine, session, "hello", None)
            await engine.create(session)
            await engine.create(other)
            with pytest.raises(UnknownModelError):
                await turn(engine, session, "hello", None, model="nope")
            with pytest.raises(CheckpointNotFoundError):
                await turn(engine, session, "hello", "not a checkpoint")
            one = answer(await turn(engine, other, "hello", None))
            with pytest.raises(CheckpointNotFoundError):
                await turn(engine, session, "hello", one.checkpoint_id)
            opencode_session = one.checkpoint_id.split(":")[0]
            with pytest.raises(CheckpointNotFoundError):
                await turn(engine, other, "hello", f"{opencode_session}:msg_none")
            assert engine.sessions(session) == []
        finally:
            await engine.close()

    asyncio.run(refusals())
