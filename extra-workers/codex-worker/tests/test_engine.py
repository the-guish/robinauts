# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The engine against a stand-in of ``codex app-server`` (stand_in_codex.py), run as a process
of its own the way Codex is: one conversation's turns, continued, regenerated, failed,
cancelled and forgotten, and what the engine refuses before it runs anything."""

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
from robinauts_codex_worker.engine import ENGINE, CodexEngine, init_codex
from robinauts_codex_worker.settings import Settings, SettingsError, settings_from

STAND_IN = Path(__file__).with_name("stand_in_codex.py")


class Keys(ProviderKeyLookup):
    def key_for(self, provider_id: str) -> str:
        return "the-key"


class Secrets(ToolSecretLookup):
    def secret_for(self, server_id: str) -> str:
        return "the-secret"


SETTINGS = EngineSettings(
    models=ModelsConfig(
        providers={
            "gw": ModelProviderConfig(
                "gw", ProviderKind.OPENAI_COMPATIBLE, "GW_KEY", base_url="http://gw/v1"
            ),
            "claude": ModelProviderConfig("claude", ProviderKind.ANTHROPIC, "ANTHROPIC_API_KEY"),
        },
        models={
            "gpt": ModelConfig("gpt", "gw", "gpt-5.5"),
            "opus": ModelConfig("opus", "claude", "claude-opus-5-5"),
        },
        tool_servers={"kb": ToolServerConfig("kb", "https://kb/mcp", "KB")},
    ),
    keys=Keys(),
    tool_secrets=Secrets(),
)
NOTHING = StorageConfig(StorageKind.IN_PROCESS, {})


async def _echo(arguments: Mapping[str, Any]) -> str:
    if arguments.get("fail"):
        raise FunctionToolError("asked to fail")
    return f"found {arguments.get('q')}"


FIND = FunctionTool("find", "Find it", {"type": "object"}, _echo)
AGENT = AgentDefinition("You are a robinaut.", tools=("kb",), functions=(FIND,))


def container(tmp_path: Path) -> Settings:
    cli = tmp_path / "codex"
    cli.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{STAND_IN}" "$@"\n')
    cli.chmod(cli.stat().st_mode | stat.S_IXUSR)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    return settings_from(
        {
            "ROBINAUTS_CODEX_AUTH": "deployed",
            "ROBINAUTS_CODEX_WORKSPACES": str(tmp_path / "workspaces"),
            "ROBINAUTS_CODEX_CLI_PATH": str(cli),
            "CODEX_HOME": str(home),
        }
    )


async def turn(
    engine: CodexEngine,
    session: uuid.UUID,
    prompt: str,
    checkpoint: str | None,
    *,
    timeout: float = 30.0,
    agent: AgentDefinition = AGENT,
) -> list[Event]:
    return [
        event
        async for event in engine.stream(
            session,
            agent,
            prompt,
            model="gpt",
            checkpoint_id=checkpoint,
            timeout_seconds=timeout,
        )
    ]


def answer(events: list[Event]) -> Done:
    done = events[-1]
    assert isinstance(done, Done)
    assert "".join(e.text for e in events if isinstance(e, TextDelta)) == done.text
    return done


def log(home: Path) -> list[dict[str, Any]]:
    path = home / "stand-in.log.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_the_entry_point_builds_the_engine_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (found,) = entry_points(group=ENTRY_POINTS, name=ENGINE)
    assert found.load() is init_codex
    assert installed()[ENGINE] is init_codex
    monkeypatch.setenv("ROBINAUTS_CODEX_AUTH", "deployed")
    monkeypatch.setenv("ROBINAUTS_CODEX_WORKSPACES", str(tmp_path))
    engine = init_codex(SETTINGS, NOTHING)
    assert isinstance(engine, CodexEngine)
    assert engine.kinds() == {ProviderKind.OPENAI, ProviderKind.OPENAI_COMPATIBLE}
    monkeypatch.setenv("ROBINAUTS_CODEX_AUTH", "personal")
    assert init_codex(SETTINGS, NOTHING).kinds() == {ProviderKind.OPENAI}
    monkeypatch.delenv("ROBINAUTS_CODEX_AUTH")
    with pytest.raises(SettingsError, match="ROBINAUTS_CODEX_AUTH"):
        init_codex(SETTINGS, NOTHING)


def test_a_conversation_on_codex(tmp_path: Path) -> None:
    """One conversation the way the platform holds it: a first turn, a second that continues
    it, a regenerate of the first answer, tools, a failed turn and one after it, a cancelled
    turn, and the conversation forgotten."""

    async def journey() -> None:
        settings = container(tmp_path)
        engine = CodexEngine(SETTINGS, settings)
        await engine.setup()
        session = uuid.uuid4()
        await engine.create(session)
        assert await engine.exists(session)
        with pytest.raises(SessionExistsError):
            await engine.create(session)

        first = await turn(engine, session, "hello", None)
        assert first[0] == ReasoningDelta("pondering")
        one = answer(first)
        assert one.text == "You said hello; before: []"

        two = answer(await turn(engine, session, "again", one.checkpoint_id))
        assert two.text == "You said again; before: ['hello']"
        assert two.checkpoint_id.split(":")[0] == one.checkpoint_id.split(":")[0]

        # A regenerate of the second answer: a fork through the first turn, on a new thread.
        regenerated = answer(await turn(engine, session, "instead", one.checkpoint_id))
        assert regenerated.text == "You said instead; before: ['hello']"
        assert regenerated.checkpoint_id.split(":")[0] != one.checkpoint_id.split(":")[0]
        # The first thread is still there, and goes on from its own answer.
        three = answer(await turn(engine, session, "more", two.checkpoint_id))
        assert three.text == "You said more; before: ['hello', 'again']"

        # A tool run here, called back by Codex, and a failure the tool means for the model.
        tooled = await turn(engine, session, 'tool find {"q": "it"}', three.checkpoint_id)
        assert tooled[:2] == [
            ToolCall("call-1", "find", {"q": "it"}),
            ToolResult("call-1", "find", "found it", False, None),
        ]
        assert answer(tooled).text == "found it"
        failed = await turn(engine, session, 'tool find {"fail": true}', three.checkpoint_id)
        assert failed[1] == ToolResult("call-1", "find", "asked to fail", True, None)

        shell = await turn(engine, session, "shell", three.checkpoint_id)
        assert shell[:2] == [
            ToolCall("call-2", "shell", {"command": "ls", "cwd": "/w"}),
            ToolResult("call-2", "shell", "a\n", False, None),
        ]

        # A refused key: the turn fails, saying which variable to fix, and never the key.
        with pytest.raises(EngineError, match="GW_KEY") as refused:
            await turn(engine, session, "unauthorized", three.checkpoint_id)
        assert "the-key" not in str(refused.value)
        # The next turn from the last answer does not remember the failed one.
        after = answer(await turn(engine, session, "after", three.checkpoint_id))
        assert after.text == "You said after; before: ['hello', 'again', 'more']"

        # A turn past its deadline is interrupted, and ends by raising TimeoutError.
        with pytest.raises(TimeoutError):
            await turn(engine, session, "sleep", after.checkpoint_id, timeout=2.0)
        interrupts = [e for e in log(settings.home) if e.get("method") == "turn/interrupt"]
        assert len(interrupts) == 1

        # The command line carries no secret; the secrets are in Codex's environment.
        started = [e for e in log(settings.home) if "overrides" in e]
        assert started
        assert all("the-secret" not in json.dumps(e) for e in started)
        assert started[0]["variables"] == ["ROBINAUTS_CODEX_TOOL_HEADER_0"]
        instructions = [
            e["params"]["developerInstructions"]
            for e in log(settings.home)
            if e.get("method") in ("thread/start", "thread/resume", "thread/fork")
        ]
        assert set(instructions) == {"You are a robinaut."}

        threads = engine.threads(session)
        assert len(threads) >= 3
        # Every thread, forks too, runs on the agent's provider, never on Codex's default.
        state = json.loads((settings.home / "stand-in.json").read_text())
        assert {state["threads"][t]["provider"] for t in threads} == {"robinauts_gw"}
        await engine.forget(session)
        assert not await engine.exists(session)
        assert engine.threads(session) == []
        state = json.loads((settings.home / "stand-in.json").read_text())
        assert not set(threads) & set(state["threads"])
        await engine.forget(session)

    asyncio.run(journey())


def test_what_the_engine_refuses_before_it_runs_anything(tmp_path: Path) -> None:
    async def refusals() -> None:
        engine = CodexEngine(SETTINGS, container(tmp_path))
        await engine.setup()
        session, other = uuid.uuid4(), uuid.uuid4()
        with pytest.raises(SessionNotFoundError):
            await turn(engine, session, "hello", None)
        await engine.create(session)
        await engine.create(other)
        with pytest.raises(UnknownModelError):
            async for _ in engine.stream(
                session, AGENT, "x", model="nope", checkpoint_id=None, timeout_seconds=5
            ):
                pass
        with pytest.raises(EngineError, match="cannot reach"):
            async for _ in engine.stream(
                session, AGENT, "x", model="opus", checkpoint_id=None, timeout_seconds=5
            ):
                pass
        with pytest.raises(CheckpointNotFoundError):
            await turn(engine, session, "hello", "not a checkpoint")
        one = answer(await turn(engine, other, "hello", None))
        # Another conversation's checkpoint is refused, and nothing is written.
        with pytest.raises(CheckpointNotFoundError):
            await turn(engine, session, "hello", one.checkpoint_id)
        thread = one.checkpoint_id.split(":")[0]
        with pytest.raises(CheckpointNotFoundError):
            await turn(engine, other, "hello", f"{thread}:no-such-turn")
        assert engine.threads(session) == []
        # A code task this worker cannot set up ends the turn before Codex starts.
        with pytest.raises(EngineError, match="code_task/1"):
            async for _ in engine.stream(
                session,
                AGENT,
                "x",
                model="gpt",
                checkpoint_id=None,
                timeout_seconds=5,
                task_payload={"task_key": "code_task/1", "repositories": {}},
            ):
                pass

    asyncio.run(refusals())
