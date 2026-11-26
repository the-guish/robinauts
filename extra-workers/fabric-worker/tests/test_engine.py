# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The engine's decisions, against a stand-in of Fabric's runtimes: which turn runs on the live
runtime and which starts a seeded one, how a failure, a timeout and a resume end, what is
refused, and which runtimes are stopped to make room. No Fabric and no Claude Code runs here;
test_fabric.py runs them."""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from robinauts.agent_engines.contract.domain import (
    AgentDefinition,
    CheckpointNotFoundError,
    Done,
    EngineError,
    SessionExistsError,
    SessionNotFoundError,
    TextDelta,
    ToolCall,
    ToolResult,
    UnknownModelError,
)
from robinauts_fabric_worker.engine import FabricEngine
from robinauts_fabric_worker.history import SEED_OPENING
from robinauts_fabric_worker.settings import Settings
from tests.conftest import KEY, MODEL, engine_settings

AGENT = AgentDefinition("You are a robinaut.", tools=("docs",))


def answered(text: str, *, status: str = "succeeded", **output: Any) -> dict[str, Any]:
    events = [
        {"type": "SystemMessage", "message": {"subtype": "init", "data": {}}},
        {"type": "AssistantMessage", "message": {"content": [{"text": text}]}},
    ]
    return {"status": status, "output": {"response": text, "events": events, **output}}


class StandInRuntime:
    def __init__(self, config: Any, answers: list[Any]) -> None:
        self.config = config
        self.asked: list[str] = []
        self.stopped = False
        self._answers = answers

    async def invoke(self, *, input: Any = None) -> Any:
        self.asked.append(input)
        answer = self._answers.pop(0) if self._answers else None
        if isinstance(answer, BaseException):
            raise answer
        if answer == "hang":
            await asyncio.sleep(60)
        return answer if isinstance(answer, dict) else answered(f"re: {input}")

    async def stop(self) -> None:
        self.stopped = True


class Starter:
    def __init__(self) -> None:
        self.started: list[StandInRuntime] = []
        self.answers: list[Any] = []

    async def __call__(self, config: Any, base_dir: Path) -> StandInRuntime:
        runtime = StandInRuntime(config, self.answers)
        self.started.append(runtime)
        return runtime


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


async def made(worker: Settings, **kwargs: Any) -> tuple[FabricEngine, Starter, uuid.UUID]:
    starter = Starter()
    engine = FabricEngine(engine_settings(), worker, starter, **kwargs)
    await engine.setup()
    session = uuid.uuid4()
    await engine.create(session)
    return engine, starter, session


async def turn(
    engine: FabricEngine,
    session: uuid.UUID,
    prompt: str,
    checkpoint: str | None,
    *,
    model: str = MODEL,
    resume: bool = False,
    timeout: float = 5.0,
) -> list[Any]:
    return [
        event
        async for event in engine.stream(
            session,
            AGENT,
            prompt,
            model=model,
            checkpoint_id=checkpoint,
            timeout_seconds=timeout,
            resume=resume,
        )
    ]


def instructions(runtime: StandInRuntime) -> str:
    system = runtime.config.instructions
    return "" if system is None else system.system.content


def test_continuing_runs_on_the_live_runtime_and_an_edit_starts_a_seeded_one(
    worker: Settings,
) -> None:
    async def run() -> None:
        engine, starter, session = await made(worker)
        first = (await turn(engine, session, "one", None))[-1]
        assert first == Done("re: one", first.checkpoint_id)
        second = (await turn(engine, session, "two", first.checkpoint_id))[-1]
        assert len(starter.started) == 1
        assert starter.started[0].asked == ["one", "two"]
        assert SEED_OPENING not in instructions(starter.started[0])

        await turn(engine, session, "two, again", first.checkpoint_id)
        assert len(starter.started) == 2
        assert starter.started[0].stopped
        seeded = instructions(starter.started[1])
        assert SEED_OPENING in seeded
        assert "re: one" in seeded
        assert "re: two" not in seeded

        # The runtime that answered last is the live one; the one before it is not.
        await turn(engine, session, "three", second.checkpoint_id)
        assert len(starter.started) == 3
        assert "re: two" in instructions(starter.started[2])

    asyncio.run(run())


def test_another_model_starts_a_runtime_of_its_own(worker: Settings) -> None:
    async def run() -> None:
        engine, starter, session = await made(worker)
        first = (await turn(engine, session, "one", None))[-1]
        await turn(engine, session, "two", first.checkpoint_id, model="other")
        assert len(starter.started) == 2
        assert starter.started[1].config.models["default"].model == "claude-opus-5-5"

    asyncio.run(run())


def test_a_turn_writes_the_permissions_and_keeps_the_key_out_of_the_config(
    worker: Settings,
) -> None:
    async def run() -> None:
        engine, starter, session = await made(worker)
        await turn(engine, session, "one", None)
        rules = json.loads(
            (engine.workspace(session) / ".claude" / "settings.local.json").read_text()
        )
        assert "mcp__docs" in rules["permissions"]["allow"]
        assert "mcp__docs__delete" in rules["permissions"]["deny"]
        assert KEY not in starter.started[0].config.model_dump_json()

    asyncio.run(run())


def test_the_events_come_from_the_result(worker: Settings) -> None:
    async def run() -> None:
        engine, starter, session = await made(worker)
        starter.answers.append(
            {
                "status": "succeeded",
                "output": {
                    "response": "done",
                    "events": [
                        {
                            "type": "AssistantMessage",
                            "message": {
                                "content": [{"id": "t1", "name": "Bash", "input": {"c": "ls"}}]
                            },
                        },
                        {
                            "type": "UserMessage",
                            "message": {
                                "content": [
                                    {"tool_use_id": "t1", "content": "a", "is_error": False}
                                ]
                            },
                        },
                        {"type": "AssistantMessage", "message": {"content": [{"text": "done"}]}},
                    ],
                },
            }
        )
        seen = await turn(engine, session, "list", None)
        assert seen[:-1] == [
            ToolCall("t1", "Bash", {"c": "ls"}),
            ToolResult("t1", "Bash", "a", False),
            TextDelta("done"),
        ]

    asyncio.run(run())


def test_a_failed_turn_ends_its_runtime_and_is_not_remembered(worker: Settings) -> None:
    async def run() -> None:
        engine, starter, session = await made(worker)
        first = (await turn(engine, session, "one", None))[-1]
        starter.answers.append(
            {"status": "failed", "error": {"code": "claude_failed", "message": "it broke"}}
        )
        with pytest.raises(EngineError, match="it broke"):
            await turn(engine, session, "two", first.checkpoint_id)
        assert starter.started[0].stopped
        assert engine.live(session) is None
        await turn(engine, session, "two", first.checkpoint_id)
        assert "re: one" in instructions(starter.started[1])

    asyncio.run(run())


def test_a_refused_key_says_which_variable(worker: Settings) -> None:
    async def run() -> None:
        engine, starter, session = await made(worker)
        starter.answers.append(
            {
                "status": "failed",
                "error": {"code": "claude_result_failed", "metadata": {"api_error_status": 401}},
            }
        )
        with pytest.raises(EngineError, match="LOCAL_KEY"):
            await turn(engine, session, "one", None)

    asyncio.run(run())


def test_a_raised_invocation_and_a_timeout_end_the_runtime(worker: Settings) -> None:
    async def run() -> None:
        engine, starter, session = await made(worker)
        starter.answers.append(RuntimeError("host crashed"))
        with pytest.raises(RuntimeError):
            await turn(engine, session, "one", None)
        assert starter.started[0].stopped
        starter.answers.append("hang")
        with pytest.raises(TimeoutError):
            await turn(engine, session, "one", None, timeout=0.05)
        assert starter.started[1].stopped
        assert engine.live(session) is None

    asyncio.run(run())


def test_resuming_a_finished_turn_gives_its_answer_again(worker: Settings) -> None:
    async def run() -> None:
        engine, starter, session = await made(worker)
        first = await turn(engine, session, "one", None)
        again = await turn(engine, session, "one", None, resume=True)
        assert again == first
        assert starter.started[0].asked == ["one"]
        with pytest.raises(EngineError, match="another question"):
            await turn(engine, session, "other", None, resume=True)
        # A resume with nothing saved after its checkpoint runs the turn.
        continued = await turn(engine, session, "two", first[-1].checkpoint_id, resume=True)
        assert continued[-1].text == "re: two"

    asyncio.run(run())


def test_refusals(worker: Settings) -> None:
    async def run() -> None:
        engine, _, session = await made(worker)
        with pytest.raises(SessionExistsError):
            await engine.create(session)
        with pytest.raises(SessionNotFoundError):
            await turn(engine, uuid.uuid4(), "one", None)
        with pytest.raises(CheckpointNotFoundError):
            await turn(engine, session, "one", str(uuid.uuid4()))
        with pytest.raises(UnknownModelError):
            await turn(engine, session, "one", None, model="nope")
        with pytest.raises(EngineError, match="cannot reach"):
            await turn(engine, session, "one", None, model="gpt")
        other = uuid.uuid4()
        await engine.create(other)
        theirs = (await turn(engine, other, "one", None))[-1]
        with pytest.raises(CheckpointNotFoundError):
            await turn(engine, session, "one", theirs.checkpoint_id)

    asyncio.run(run())


def test_idle_and_surplus_runtimes_are_stopped_to_make_room(worker: Settings) -> None:
    async def run() -> None:
        clock = Clock()
        engine, starter, first = await made(
            replace(worker, max_runtimes=2, idle_seconds=100), clock=clock
        )
        sessions = [first]
        for _ in range(2):
            session = uuid.uuid4()
            await engine.create(session)
            sessions.append(session)
        await turn(engine, sessions[0], "a", None)
        clock.now = 10
        await turn(engine, sessions[1], "b", None)
        clock.now = 20
        # At the cap: the least recently used goes.
        await turn(engine, sessions[2], "c", None)
        assert [r.stopped for r in starter.started] == [True, False, False]
        clock.now = 200
        # Past their idle time: both go, though one would make room.
        await turn(engine, sessions[0], "a", None)
        assert [r.stopped for r in starter.started] == [True, True, True, False]

    asyncio.run(run())


def test_forget_and_close(worker: Settings) -> None:
    async def run() -> None:
        engine, starter, session = await made(worker)
        await turn(engine, session, "one", None)
        await engine.forget(session)
        assert starter.started[0].stopped
        assert not await engine.exists(session)
        assert not engine.workspace(session).exists()
        await engine.forget(session)
        other = uuid.uuid4()
        await engine.create(other)
        await turn(engine, other, "one", None)
        await engine.close()
        assert starter.started[1].stopped

    asyncio.run(run())
