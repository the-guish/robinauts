# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The kataloop engine: our own agent loop, kataloop, over the store it is handed.

kataloop keeps robinauts' contract under its own names (kataloop's ``docs/specs/engine.md``), so
this is a translation: the settings into a client, a model and tool servers at every turn, and
kataloop's events and errors into robinauts'. A turn runs on a ``Kataloop`` of its own, over the
turn's client, the engine's store and the turn's toolbox: kataloop stores nothing but the memory,
so that is the same engine every time.

The store is the engine's for its lifetime. ``init_kataloop`` hands it kataloop's ``MemoryStore``
for every storage kind until ``kataloop[postgres]`` exists: **the memory lives in this
process**, and a conversation's next turn after a restart, or on another node, finds no session.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from contextlib import aclosing

import kataloop_core as kataloop

from robinauts.agent_engines.contract.domain import (
    AgentDefinition,
    CheckpointNotFoundError,
    Done,
    Event,
    ProviderKind,
    ReasoningDelta,
    SessionExistsError,
    SessionNotFoundError,
    TextDelta,
    ToolCall,
    ToolResult,
)
from robinauts.agent_engines.contract.ports import AgentEngine, EngineSettings
from robinauts.agent_engines.kataloop_engine.clients import KINDS, client_for
from robinauts.agent_engines.kataloop_engine.tools import tools_for


class KataloopEngine(AgentEngine):
    def __init__(self, settings: EngineSettings, store: kataloop.Store) -> None:
        self._settings = settings
        self._store = store

    def kinds(self) -> frozenset[ProviderKind]:
        return frozenset(KINDS)

    # A Kataloop's setup, create, exists, fork and forget are its store's, and need no model
    # client: they are called on the store, with kataloop's errors told in robinauts' names.

    async def setup(self) -> None:
        await self._store.setup()

    async def create(self, session_id: uuid.UUID) -> None:
        try:
            await self._store.create(session_id)
        except kataloop.SessionExistsError as error:
            raise SessionExistsError(*error.args) from error

    async def exists(self, session_id: uuid.UUID) -> bool:
        return await self._store.exists(session_id)

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
        client, spec = client_for(model, self._settings)
        try:
            servers, toolbox = tools_for(agent, self._settings)
            turn = kataloop.Kataloop(client, self._store, toolbox).stream(
                session_id,
                kataloop.AgentDefinition(agent.system_prompt, servers),
                prompt,
                model=spec,
                checkpoint_id=checkpoint_id,
                timeout=timeout_seconds,
                resume=resume,
            )
            # Closing this turn closes kataloop's, which closes the model's stream.
            async with aclosing(turn) as events:
                async for event in events:
                    yield event_of(event)
        except kataloop.SessionNotFoundError as error:
            raise SessionNotFoundError(*error.args) from error
        except kataloop.CheckpointNotFoundError as error:
            raise CheckpointNotFoundError(*error.args) from error
        finally:
            # The client is the turn's: its connections go with it.
            await client.close()

    async def fork(self, source_id: uuid.UUID, target_id: uuid.UUID, *, checkpoint_id: str) -> None:
        try:
            await self._store.copy(source_id, target_id, checkpoint_id=checkpoint_id)
        except kataloop.SessionNotFoundError as error:
            raise SessionNotFoundError(*error.args) from error
        except kataloop.CheckpointNotFoundError as error:
            raise CheckpointNotFoundError(*error.args) from error
        except kataloop.SessionExistsError as error:
            raise SessionExistsError(*error.args) from error

    async def forget(self, session_id: uuid.UUID) -> None:
        await self._store.delete(session_id)


def event_of(event: kataloop.Event) -> Event:
    """kataloop's event under robinauts' name; ``Done`` without the turn's usage, which the
    contract does not carry yet."""
    match event:
        case kataloop.TextDelta(text=text):
            return TextDelta(text)
        case kataloop.ReasoningDelta(text=text):
            return ReasoningDelta(text)
        case kataloop.ToolCall(call_id=call_id, name=name, arguments=arguments):
            return ToolCall(call_id, name, dict(arguments))
        case kataloop.ToolResult(call_id=call_id, name=name, output=output, is_error=is_error):
            return ToolResult(call_id, name, output, is_error)
        case kataloop.Done(text=text, checkpoint_id=checkpoint):
            return Done(text=text, checkpoint_id=checkpoint)
    raise TypeError(f"not an event of kataloop's: {event!r}")
