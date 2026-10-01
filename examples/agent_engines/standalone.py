# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Have the examples' conversation through the ``AgentEngine`` port, in one process.

The ``main.py`` of `agent-framework-examples
<https://github.com/the-guish/agent-framework-examples>`_, as the platform
would write it against ``robinauts.ports.agent_engine.AgentEngine``: the
same two questions -- the warmer of three cities, then the coolest, which
only the memory can answer -- through each engine in turn.

Not runnable until an engine implements the port: ``ENGINES`` is empty, and
an adapter is added to it the day it does. What is here is the caller's side,
which is the point: a conversation is created on purpose, the first turn is
streamed with no checkpoint, the ``checkpoint_id`` that ``Done`` carries is
kept the way the transcript keeps it on the answer, and the second turn is
told to continue from it. The engine is never asked to remember where the
conversation stands.

    export ANTHROPIC_API_KEY=...
    uv run --project backend examples/agent_engines/standalone.py
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Callable

from robinauts.domain import (
    AgentDefinition,
    Done,
    Engine,
    Event,
    TextDelta,
    ToolCall,
    ToolResult,
)
from robinauts.ports.agent_engine import AgentEngine

# Filled in the day an adapter implements the port:
#   "langgraph": LangGraphEngine (robinauts.adapters.agents.langgraph),
#   "pydantic-ai": PydanticAIEngine (robinauts.adapters.agents.pydantic_ai).
ENGINES: dict[str, Callable[[], AgentEngine]] = {}

MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5")
TURN_SECONDS = 120.0

AGENT = AgentDefinition(
    id="weather",
    title="Weather",
    system_prompt="",
    model=MODEL,
    engine=Engine.LANGGRAPH,
    # The weather tool of the examples would be an MCP server named here.
    tools=(),
)


def main() -> None:
    print(f"Model: {MODEL}")
    if not ENGINES:
        print("No engine implements the port yet; nothing to run.")
    for name, make in ENGINES.items():
        print(f"\n=== {name}")
        try:
            asyncio.run(converse(make()))
        except Exception as error:  # report it and go on with the next engine
            print(f"FAILED: {type(error).__name__}: {error}")


async def converse(engine: AgentEngine) -> None:
    conversation_id = uuid.uuid4()
    await engine.create(conversation_id)

    # The first turn of a conversation continues from nothing.
    first = await show(
        engine.stream(
            conversation_id,
            AGENT,
            "What's the warmer city righ now of Tampa, Madrid, Montevideo? Answer in one word",
            model=MODEL,
            checkpoint_id=None,
            timeout=TURN_SECONDS,
        )
    )
    # The platform stores the answer with `first` on it. That is all it keeps of the memory,
    # and it is what the next turn is told to continue from.
    print(f"(the answer is stored with checkpoint {first})")

    await show(
        engine.stream(
            conversation_id,
            AGENT,
            "What's the coolest? One word",
            model=MODEL,
            checkpoint_id=first,
            timeout=TURN_SECONDS,
        )
    )


async def show(events: AsyncIterator[Event]) -> str | None:
    """Print a turn as it streams; hand back the checkpoint id its answer is stored with."""
    checkpoint_id = None
    async for event in events:
        match event:
            case TextDelta(text=text):
                print(text, end="", flush=True)
            case ToolCall(call_id=call_id, name=name, arguments=arguments):
                print(f"\n  [call {call_id}] {name}({arguments})")
            case ToolResult(call_id=call_id, name=name, output=output):
                print(f"  [result {call_id}] {name} -> {output}")
            case Done(text=text, checkpoint_id=checkpoint_id):
                print(f"\n  [done] {text}")
    return checkpoint_id


if __name__ == "__main__":
    main()
