# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""An engine that calls its one tool on every turn and answers a fixed string plus the result.

A prompt that starts with ``poison`` ends its turn badly instead: the tool returns an error and
the engine raises, as a real engine does when a tool keeps failing.

It keeps nothing: every session exists, every checkpoint is accepted, each turn hands out a new
one, and no turn reads an earlier one. So it does not keep the memory half of the contract, and
is for quick smoke tests of the interface and for tests that need a turn and nothing more.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator

from robinauts.agent_engines.contract.domain import (
    AgentDefinition,
    Done,
    EngineError,
    Event,
    ProviderKind,
    TextDelta,
    ToolCall,
    ToolResult,
)
from robinauts.agent_engines.contract.ports import AgentEngine

TOOL = "echo"
ANSWER = "The tool said: "
POISON = "poison"
POISONED = "the message is poisoned"


def echo(text: str) -> str:
    """The one tool: what it is given."""
    return text


class EchoEngine(AgentEngine):
    def kinds(self) -> frozenset[ProviderKind]:
        return frozenset(ProviderKind)

    async def setup(self) -> None:
        # Nothing to set up: it keeps no memory.
        pass

    async def create(self, session_id: uuid.UUID) -> None:
        # Nothing to record: every session exists.
        pass

    async def exists(self, session_id: uuid.UUID) -> bool:
        return True

    async def stream(
        self,
        session_id: uuid.UUID,
        agent: AgentDefinition,
        prompt: str,
        *,
        model: str,
        checkpoint_id: str | None,
        timeout_seconds: float,
        resume: bool = False,
    ) -> AsyncGenerator[Event, None]:
        call_id = f"call-{uuid.uuid4().hex[:8]}"
        yield ToolCall(call_id=call_id, name=TOOL, raw_input={"text": prompt})
        if prompt.startswith(POISON):
            yield ToolResult(call_id=call_id, name=TOOL, raw_output=POISONED, failed=True)
            raise EngineError(f"Tool {TOOL!r} failed: {POISONED}")
        result = echo(prompt)
        yield ToolResult(call_id=call_id, name=TOOL, raw_output=result)
        yield TextDelta(text=ANSWER)
        yield TextDelta(text=result)
        yield Done(text=ANSWER + result, checkpoint_id=str(uuid.uuid4()))

    async def fork(self, source_id: uuid.UUID, target_id: uuid.UUID, *, checkpoint_id: str) -> None:
        # Nothing to copy: no turn reads an earlier one.
        pass

    async def forget(self, session_id: uuid.UUID) -> None:
        # Nothing to delete: it keeps no memory.
        pass
