# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""An engine that calls its one tool on every turn and answers a fixed string plus the result.

Its tool call's id counts the turns it remembers, this one included: ``call-1`` on a turn
that continues from no checkpoint, ``call-3`` on one that continues from the second.

A prompt that starts with ``poison`` ends its turn badly instead: the tool returns an error and
the engine raises, as a real engine does when a tool keeps failing.

It keeps the contract's memory and nothing else: which sessions exist and which
checkpoints each holds, in this process. No turn reads an earlier one.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator

from robinauts.agent_engines.contract.domain import (
    AgentDefinition,
    CheckpointNotFoundError,
    Done,
    EngineError,
    Event,
    ProviderKind,
    ResumeMismatchError,
    SessionExistsError,
    SessionNotFoundError,
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
    def __init__(self) -> None:
        self._sessions: dict[uuid.UUID, list[str]] = {}
        self._last_prompt: dict[uuid.UUID, str] = {}
        self._remembers: dict[str, int] = {}
        """How many turns each checkpoint remembers, its own included; a fork keeps the ids."""

    def kinds(self) -> frozenset[ProviderKind]:
        return frozenset(ProviderKind)

    async def setup(self) -> None:
        # Nothing to set up: its sessions live in this process and start empty.
        pass

    async def create(self, session_id: uuid.UUID) -> None:
        if session_id in self._sessions:
            raise SessionExistsError(str(session_id))
        self._sessions[session_id] = []

    async def exists(self, session_id: uuid.UUID) -> bool:
        return session_id in self._sessions

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
        max_model_calls: int | None = None,
    ) -> AsyncGenerator[Event, None]:
        checkpoints = self._sessions.get(session_id)
        if checkpoints is None:
            raise SessionNotFoundError(str(session_id))
        if checkpoint_id is not None and checkpoint_id not in checkpoints:
            raise CheckpointNotFoundError(checkpoint_id)
        if resume and self._last_prompt.get(session_id, prompt) != prompt:
            raise ResumeMismatchError("resume with another prompt")
        self._last_prompt[session_id] = prompt

        remembered = 0 if checkpoint_id is None else self._remembers[checkpoint_id]
        call_id = f"call-{remembered + 1}"
        yield ToolCall(call_id=call_id, name=TOOL, arguments={"text": prompt})
        if prompt.startswith(POISON):
            yield ToolResult(call_id=call_id, name=TOOL, output=POISONED, is_error=True)
            raise EngineError(f"Tool {TOOL!r} failed: {POISONED}")
        result = echo(prompt)
        yield ToolResult(call_id=call_id, name=TOOL, output=result)
        yield TextDelta(text=ANSWER)
        yield TextDelta(text=result)
        new = str(uuid.uuid4())
        checkpoints.append(new)
        self._remembers[new] = remembered + 1
        yield Done(text=ANSWER + result, checkpoint_id=new)

    async def fork(self, source_id: uuid.UUID, target_id: uuid.UUID, *, checkpoint_id: str) -> None:
        source = self._sessions.get(source_id)
        if source is None:
            raise SessionNotFoundError(str(source_id))
        if checkpoint_id not in source:
            raise CheckpointNotFoundError(checkpoint_id)
        if target_id in self._sessions:
            raise SessionExistsError(str(target_id))
        self._sessions[target_id] = source[: source.index(checkpoint_id) + 1]

    async def forget(self, session_id: uuid.UUID) -> None:
        self._sessions.pop(session_id, None)
        self._last_prompt.pop(session_id, None)
