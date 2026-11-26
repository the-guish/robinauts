# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The engine on the real thing: NeMo Fabric starts its Claude adapter's host, which runs Claude
Code, the binary the Claude Agent SDK bundles, against a stand-in of Anthropic's Messages API on
this machine (fake_anthropic.py). No key, no network, no Docker.

A conversation of three turns and an edit: the second turn is invoked on the live runtime, whose
Claude Code remembers the first; a turn calls Claude Code's ``Bash`` in the conversation's
workspace and the call and its result are events; an edit under the first answer, and a turn
after the engine was built again, as at a restart, each start a runtime seeded with the turns up
to their checkpoint, and nothing after it.
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path

import pytest

from robinauts.agent_engines.contract.domain import (
    AgentDefinition,
    Done,
    Event,
    TextDelta,
    ToolCall,
    ToolResult,
)
from robinauts_fabric_worker.engine import ADAPTER_PYTHON_VARIABLE, FabricEngine
from robinauts_fabric_worker.history import SEED_OPENING
from robinauts_fabric_worker.settings import Settings
from tests.conftest import KEY, MODEL, engine_settings
from tests.fake_anthropic import FakeAnthropicServer, asked, system_text

AGENT = AgentDefinition("You are a robinaut.")
TIMEOUT = 120.0


@pytest.fixture
def fake() -> FakeAnthropicServer:
    with FakeAnthropicServer() as server:
        yield server


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Claude Code's configuration in a folder of the test's, and the adapter host on this
    environment's interpreter."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv(ADAPTER_PYTHON_VARIABLE, sys.executable)


async def turn(
    engine: FabricEngine, session: uuid.UUID, prompt: str, checkpoint: str | None
) -> tuple[list[Event], Done]:
    seen = [
        event
        async for event in engine.stream(
            session,
            AGENT,
            prompt,
            model=MODEL,
            checkpoint_id=checkpoint,
            timeout_seconds=TIMEOUT,
        )
    ]
    done = seen[-1]
    assert isinstance(done, Done)
    return seen[:-1], done


def questions(fake: FakeAnthropicServer) -> list[str]:
    return [asked(request["messages"]) for request in fake.conversations()]


def test_a_conversation_through_fabric(fake: FakeAnthropicServer, worker: Settings) -> None:
    async def run() -> None:
        settings = engine_settings(fake.base_url)
        engine = FabricEngine(settings, worker)
        await engine.setup()
        session = uuid.uuid4()
        await engine.create(session)
        try:
            events, first = await turn(engine, session, "hello one", None)
            assert first.text == "hello one"
            assert events == [TextDelta("hello one")]
            # The key the engine's lookup gave, and no other, reached the provider.
            assert fake.keys == {KEY}
            started = engine.live(session)

            _, second = await turn(engine, session, "hello two", first.checkpoint_id)
            assert second.text == "hello two"
            assert engine.live(session) is started
            # The live Claude Code remembered the first turn.
            last = fake.conversations()[-1]["messages"]
            assert [m["role"] for m in last if m["role"] != "system"][:3] == [
                "user",
                "assistant",
                "user",
            ]

            events, third = await turn(
                engine, session, "bash: touch made.txt && echo made", second.checkpoint_id
            )
            calls = [e for e in events if isinstance(e, ToolCall)]
            results = [e for e in events if isinstance(e, ToolResult)]
            assert [c.name for c in calls] == ["Bash"]
            assert results == [ToolResult(calls[0].call_id, "Bash", "made", False)]
            assert third.text == "ran: made"
            assert (engine.workspace(session) / "made.txt").is_file()

            # An edit under the first answer: a new runtime, seeded with the first turn alone.
            _, edited = await turn(engine, session, "hello again", first.checkpoint_id)
            assert edited.text == "hello again"
            assert engine.live(session) is not started
            system = system_text(fake.conversations()[-1])
            assert SEED_OPENING in system
            assert "hello one" in system
            assert "hello two" not in system
        finally:
            await engine.close()

        # Built again, as at a restart: the next turn is seeded from the log.
        again = FabricEngine(settings, worker)
        await again.setup()
        try:
            _, after = await turn(again, session, "still there?", third.checkpoint_id)
            assert after.text == "still there?"
            system = system_text(fake.conversations()[-1])
            assert "hello two" in system
            assert "ran: made" in system
        finally:
            await again.close()

    asyncio.run(run())
