# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""What crosses the engine port, as plain data. The meaning is ``docs/specs/agent-engines.md``."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class EngineError(Exception):
    pass


class SessionExistsError(EngineError):
    pass


class SessionNotFoundError(EngineError):
    pass


class CheckpointNotFoundError(EngineError):
    pass


class UnknownModelError(EngineError):
    pass


class ResumeMismatchError(EngineError):
    pass


class MissingSecretError(EngineError):
    pass


class ProviderKind(StrEnum):
    ANTHROPIC = "anthropic"
    ANTHROPIC_COMPATIBLE = "anthropic-compatible"
    OPENAI = "openai"
    OPENAI_COMPATIBLE = "openai-compatible"


@dataclass(frozen=True, slots=True)
class ModelProviderConfig:
    id: str
    kind: ProviderKind
    api_key_env: str
    base_url: str | None = None


@dataclass(frozen=True, slots=True)
class ModelConfig:
    id: str
    provider: str
    name: str
    timeout_seconds: float = 120.0
    max_output_tokens: int | None = None
    context_window: int | None = None
    title: str = ""


class ToolServerAuth(StrEnum):
    BEARER = "bearer"
    BASIC = "basic"
    HEADER = "header"
    NONE = "none"


@dataclass(frozen=True, slots=True)
class ToolServerConfig:
    id: str
    url: str
    secret_env: str = ""
    auth: ToolServerAuth = ToolServerAuth.BEARER
    user: str = ""
    timeout_seconds: float = 60.0
    # The header `auth = "header"` sends the secret in, as it is; set for that mode alone.
    header: str = ""


@dataclass(frozen=True, slots=True)
class ModelsConfig:
    providers: Mapping[str, ModelProviderConfig] = field(default_factory=dict)
    models: Mapping[str, ModelConfig] = field(default_factory=dict)
    tool_servers: Mapping[str, ToolServerConfig] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AgentDefinition:
    system_prompt: str
    tools: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TextDelta:
    text: str


@dataclass(frozen=True, slots=True)
class ReasoningDelta:
    text: str


@dataclass(frozen=True, slots=True)
class ToolCall:
    call_id: str
    name: str
    arguments: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ToolResult:
    call_id: str
    name: str
    output: str
    is_error: bool = False


@dataclass(frozen=True, slots=True)
class Done:
    text: str
    checkpoint_id: str


Event = TextDelta | ReasoningDelta | ToolCall | ToolResult | Done
