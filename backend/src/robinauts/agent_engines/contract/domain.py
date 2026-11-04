# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""What crosses the engine port, as plain data. The meaning is ``docs/specs/agent-engines.md``."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

MODEL_RETRIES = 20
"""How many times a vendor's client retries a model call that failed with a 429, a 5xx, a timeout
or a lost connection. The clients back off from half a second to 8 s, or as long as the vendor's
``retry-after`` asks, up to a minute: about two minutes of retries at least. The turn's deadline
ends them first."""

DEFAULT_CONTEXT_WINDOW = 128_000
"""Tokens: the window of a model whose ``context_window`` is not configured and that its framework
knows nothing of."""


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
    # Tools of the server's that the agent is not given, by the server's own names.
    exclude: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ModelsConfig:
    providers: Mapping[str, ModelProviderConfig] = field(default_factory=dict)
    models: Mapping[str, ModelConfig] = field(default_factory=dict)
    tool_servers: Mapping[str, ToolServerConfig] = field(default_factory=dict)


class FunctionToolError(Exception):
    """What a function tool raises to fail a call with a message for the model: the engine hands
    the message over as a failed result, and the turn goes on. Anything else a tool raises is
    told to the model as its kind alone."""


@dataclass(frozen=True, slots=True)
class FunctionTool:
    """A tool run in this process, which the engine gives the model like any other.

    ``input_schema`` is the JSON Schema of the arguments, an object. The frameworks do not check
    the arguments against it: ``call`` is handed them as the model sent them, and checks them
    itself. What it returns is the output the model sees.
    """

    name: str
    description: str
    input_schema: Mapping[str, Any]
    call: Callable[[Mapping[str, Any]], Awaitable[str]]


@dataclass(frozen=True, slots=True)
class AgentDefinition:
    system_prompt: str
    tools: tuple[str, ...] = ()
    """The tool servers the agent may use, by id."""
    functions: tuple[FunctionTool, ...] = ()
    """The tools run in this process, bound to the turn by the caller."""


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
    raw_input: Mapping[str, Any] = field(default_factory=dict)
    """The arguments, as the model sent them."""


@dataclass(frozen=True, slots=True)
class ToolResult:
    call_id: str
    name: str
    raw_output: str
    """The output, as the model sees it."""
    failed: bool = False


@dataclass(frozen=True, slots=True)
class Done:
    text: str
    checkpoint_id: str


Event = TextDelta | ReasoningDelta | ToolCall | ToolResult | Done
