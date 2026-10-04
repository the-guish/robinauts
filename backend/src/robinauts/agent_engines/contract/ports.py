# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The agent engine port. The contract is ``docs/specs/agent-engines.md``; here are its types."""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator, Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from importlib import import_module
from typing import Any

from robinauts.agent_engines.contract.domain import (
    AgentDefinition,
    Event,
    ModelsConfig,
    ProviderKind,
    RunLimits,
)


class ProviderKeyLookup(ABC):
    __slots__ = ()

    @abstractmethod
    def key_for(self, provider_id: str) -> str:
        """``MissingSecretError`` for a provider this process has no key for."""
        raise NotImplementedError


class ToolSecretLookup(ABC):
    __slots__ = ()

    @abstractmethod
    def secret_for(self, server_id: str) -> str:
        """``MissingSecretError`` for a server this process has no secret for."""
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class EngineSettings:
    models: ModelsConfig
    keys: ProviderKeyLookup
    tool_secrets: ToolSecretLookup
    limits: RunLimits = RunLimits()


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
        """``SessionExistsError`` for a session that already exists."""
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

        ``timeout_seconds`` is what is left of the turn's deadline, which bounds the whole run;
        each call to the vendor is bounded by the model's own ``timeout_seconds`` and retried
        ``max_retries`` times. Past the deadline the engine ends the turn by raising
        ``TimeoutError``; past ``limits.max_model_calls`` calls to the model, by raising the
        framework's own error.
        Raised where iterated: ``SessionNotFoundError`` for a session not created,
        ``CheckpointNotFoundError`` for a checkpoint not held for it, ``UnknownModelError`` for
        a model the settings do not have, ``ResumeMismatchError`` for a resume of another prompt.
        """
        raise NotImplementedError

    @abstractmethod
    async def fork(self, source_id: uuid.UUID, target_id: uuid.UUID, *, checkpoint_id: str) -> None:
        """``SessionNotFoundError`` for the source, ``CheckpointNotFoundError`` for the
        checkpoint, ``SessionExistsError`` for a target that already exists."""
        raise NotImplementedError

    @abstractmethod
    async def forget(self, session_id: uuid.UUID) -> None:
        raise NotImplementedError


EngineFactory = Callable[[EngineSettings, StorageConfig], AgentEngine]


_SHIPPED: Mapping[str, tuple[str, str]] = {
    "langchain": ("robinauts.agent_engines.langchain_engine", "init_langchain"),
    "pydantic-ai": ("robinauts.agent_engines.pydantic_ai_engine", "init_pydantic_ai"),
    "echo": ("robinauts.agent_engines.echo_engine", "init_echo"),
}
"""Every engine the package ships: its name, and where its init function is."""


def installed() -> Mapping[str, EngineFactory]:
    """The engines this build can run, by the name a configuration uses.

    An engine whose package cannot be imported, because its extra is not installed, is left out.
    """
    found: dict[str, EngineFactory] = {}
    for name, (module, function) in _SHIPPED.items():
        try:
            found[name] = getattr(import_module(module), function)
        except ImportError:
            continue
    return found
