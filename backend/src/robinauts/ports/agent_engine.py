# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The agent engine port. The contract is ``docs/specs/agent-engines.md``; here are its types."""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

from robinauts.domain import AgentDefinition, Event, ModelsConfig, ProviderKind


class ProviderKeyLookup(ABC):
    __slots__ = ()

    @abstractmethod
    def key_for(self, provider_id: str) -> str:
        """``ConfigError`` for a provider this process has no key for."""
        raise NotImplementedError


class ToolSecretLookup(ABC):
    __slots__ = ()

    @abstractmethod
    def secret_for(self, server_id: str) -> str:
        """``ConfigError`` for a server this process has no secret for."""
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class EngineSettings:
    models: ModelsConfig
    keys: ProviderKeyLookup
    tool_secrets: ToolSecretLookup


class StorageKind(Enum):
    POSTGRES = "postgres"  # options: {"pool": <the connection pool>}
    LOCAL = "local"  # options: {"path": <the folder>}
    IN_MEMORY = "in-memory"  # no options


@dataclass(frozen=True, slots=True)
class StorageConfig:
    kind: StorageKind
    options: Mapping[str, Any]


class AgentEngine(ABC):
    def kinds(self) -> frozenset[ProviderKind]:
        return frozenset()

    @abstractmethod
    async def setup(self) -> None:
        raise NotImplementedError

    @abstractmethod
    async def create(self, session_id: uuid.UUID) -> None:
        """``InvalidValueError`` for a session that already exists."""
        raise NotImplementedError

    @abstractmethod
    async def exists(self, session_id: uuid.UUID) -> bool:
        raise NotImplementedError

    @abstractmethod
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
    ) -> AsyncGenerator[Event, None]:
        """Not a coroutine: everything, the refusals included, happens inside the generator.

        Past ``timeout_seconds`` the engine ends the turn by raising ``TimeoutError``.
        Raised where iterated: ``ConversationNotFoundError`` for a session not created,
        ``CheckpointNotFoundError`` for a checkpoint not held for it, ``UnknownModelError`` for
        a model the settings do not have, ``InvalidValueError`` for a resume of another prompt.
        """
        raise NotImplementedError

    @abstractmethod
    async def fork(
        self, source_id: uuid.UUID, target_id: uuid.UUID, *, checkpoint_id: str
    ) -> None:
        """``ConversationNotFoundError`` for the source, ``CheckpointNotFoundError`` for the
        checkpoint, ``InvalidValueError`` for a target that already exists."""
        raise NotImplementedError

    @abstractmethod
    async def forget(self, session_id: uuid.UUID) -> None:
        raise NotImplementedError


EngineFactory = Callable[[EngineSettings, StorageConfig], Awaitable[AgentEngine]]
