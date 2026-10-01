# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""What crosses the engine port: the settings, the agent, the events and the errors.

The meaning of each is in ``docs/specs/agent-engines.md``. The checks here are the
bounds the records keep to, taken from the platform's own, so that an engine and
its caller refuse the same things.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any
from urllib.parse import urlsplit

# --- errors -------------------------------------------------------------------


class EngineError(Exception):
    """What every error of the contract is."""


class InvalidValueError(EngineError, ValueError):
    """A value a record or an operation cannot take."""


class SessionExistsError(EngineError):
    """A session created twice, or a fork onto a session that exists."""


class SessionNotFoundError(EngineError):
    """A turn or a fork on a session the engine does not have."""


class CheckpointNotFoundError(EngineError):
    """A checkpoint the engine does not hold for that session."""


class UnknownModelError(EngineError):
    """A model the settings do not have."""


class ResumeMismatchError(EngineError):
    """A resume asked with another prompt than the interrupted turn's."""


class MissingSecretError(EngineError):
    """A provider's key or a tool server's secret the lookup does not have."""


# --- value checks -------------------------------------------------------------

NUL = "\x00"
_SURROGATE = re.compile("[\ud800-\udfff]")
JOINERS = frozenset({"‍", "︎", "️"})

MAX_PART_CHARS = 1_000_000
MAX_DATA_BYTES = 64 * 1024
MAX_DATA_DEPTH = 32
MAX_DATA_NODES = 4096


def describe(value: object) -> str:
    """What ``value`` is, for a message that must not repeat what it says."""
    if value is None:
        return "nothing"
    if isinstance(value, str):
        return f"text of {len(value)} characters"
    if isinstance(value, bytes | bytearray):
        return f"{len(value)} bytes"
    named = type(value).__name__
    article = "an" if named[:1].lower() in "aeiou" else "a"
    if isinstance(value, list | tuple | set | frozenset | dict):
        return f"{article} {named} of {len(value)}"
    return f"{article} {named}"


def checked_fragment(value: object, what: str, limit: int) -> str:
    if not isinstance(value, str):
        raise InvalidValueError(f"{what} is text, not {describe(value)}")
    if len(value) > limit:
        raise InvalidValueError(f"{what} is at most {limit} characters, not {len(value)}")
    return value


def checked_text(value: object, what: str, limit: int) -> str:
    text = checked_fragment(value, what, limit)
    if NUL in text:
        raise InvalidValueError(f"{what} cannot hold a NUL character")
    if _SURROGATE.search(text) is not None:
        raise InvalidValueError(f"{what} cannot hold an unpaired surrogate")
    return text


def checked_line(value: object, what: str, limit: int) -> str:
    text = checked_text(value, what, limit)
    if any(
        not (character.isprintable() or character == " " or character in JOINERS)
        for character in text
    ):
        raise InvalidValueError(f"{what} is one line of printable text")
    return text


def _is_storable(text: str) -> bool:
    return isinstance(text, str) and NUL not in text and _SURROGATE.search(text) is None


def _as_object(value: object) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    raise TypeError(f"{describe(value)} is not plain data")


def checked_data(
    data: object,
    what: str,
    *,
    max_bytes: int = MAX_DATA_BYTES,
    max_depth: int = MAX_DATA_DEPTH,
    max_nodes: int = MAX_DATA_NODES,
) -> dict[str, Any]:
    """``data`` as a plain ``dict`` if it is bounded plain data; ``InvalidValueError`` if not."""
    if not isinstance(data, Mapping):
        raise InvalidValueError(f"{what} is an object, not {describe(data)}")
    stack: list[tuple[object, int]] = [(data, 0)]
    nodes = 0
    while stack:
        value, depth = stack.pop()
        nodes += 1
        if depth > max_depth:
            raise InvalidValueError(f"{what} nests at most {max_depth} deep")
        if nodes > max_nodes:
            raise InvalidValueError(f"{what} holds at most {max_nodes} values")
        if isinstance(value, str):
            if not _is_storable(value):
                raise InvalidValueError(f"the text in {what} is storable, at every depth")
        elif isinstance(value, Mapping):
            for key in value:
                if not isinstance(key, str):
                    raise InvalidValueError(f"the keys of {what} are names, at every depth")
                if not _is_storable(key):
                    raise InvalidValueError(f"the text in {what} is storable, at every depth")
            stack.extend((inside, depth + 1) for inside in value.values())
        elif isinstance(value, list | tuple):
            stack.extend((inside, depth + 1) for inside in value)
        elif value is not None and not isinstance(value, bool | int | float):
            raise InvalidValueError(
                f"{what} is plain data an export can write, not {describe(value)}"
            )
    try:
        written = json.dumps(
            data, separators=(",", ":"), allow_nan=False, ensure_ascii=False, default=_as_object
        )
        size = len(written.encode("utf-8"))
    except (TypeError, ValueError, UnicodeError):
        raise InvalidValueError(f"{what} is plain data an export can write") from None
    if size > max_bytes:
        raise InvalidValueError(f"{what} is at most {max_bytes} bytes written out, not {size}")
    return deepcopy(json.loads(written))


# --- ids and names ------------------------------------------------------------

MAX_CONFIG_ID_CHARS = 40
_CONFIG_ID = re.compile(rf"[a-z0-9][a-z0-9_-]{{0,{MAX_CONFIG_ID_CHARS - 1}}}")


def checked_config_id(value: object, what: str) -> str:
    """``value`` if it is a name the configuration may use; ``InvalidValueError`` if not."""
    if not isinstance(value, str) or _CONFIG_ID.fullmatch(value) is None:
        raise InvalidValueError(
            f"{what} is a name: lower-case letters, digits, '-' and '_', at most "
            f"{MAX_CONFIG_ID_CHARS} of them, not {describe(value)}"
        )
    return value


MAX_TOOL_NAME_CHARS = 64
_TOOL_NAME = re.compile(rf"[A-Za-z0-9_-]{{1,{MAX_TOOL_NAME_CHARS}}}")
MAX_CALL_ID_CHARS = 128


def checked_tool_name(value: object, what: str) -> str:
    if not isinstance(value, str) or _TOOL_NAME.fullmatch(value) is None:
        raise InvalidValueError(
            f"{what} is letters, digits, '_' and '-', at most {MAX_TOOL_NAME_CHARS} of them,"
            f" not {describe(value)}"
        )
    return value


def checked_call_id(value: object, what: str) -> str:
    text = checked_line(value, what, MAX_CALL_ID_CHARS)
    if not text or any(character.isspace() for character in text):
        raise InvalidValueError(f"{what} is one word of printable text, not {describe(value)}")
    return text


MAX_ENV_NAME_CHARS = 120
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def is_env_name(value: object) -> bool:
    """Whether ``value`` is the name of an environment variable rather than a value."""
    return isinstance(value, str) and _ENV_NAME.fullmatch(value) is not None


LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
MAX_BASE_URL_CHARS = 500


def is_endpoint_url(value: object) -> bool:
    """Whether ``value`` is an endpoint a key may be sent to.

    ``https://`` anywhere, ``http://`` on the loopback interface only; a host, no query,
    no fragment and no user:password, since the URL is a prefix a client appends to.
    """
    if not isinstance(value, str) or not value:
        return False
    if any(character.isspace() for character in value) or not value.isascii():
        return False
    if "?" in value or "#" in value:
        return False
    try:
        split = urlsplit(value)
        _ = split.port
    except ValueError:
        return False
    if split.username is not None or split.password is not None:
        return False
    if not split.hostname:
        return False
    if split.scheme == "https":
        return True
    return split.scheme == "http" and split.hostname in LOOPBACK_HOSTS


# --- providers and models -----------------------------------------------------


class ProviderKind(StrEnum):
    ANTHROPIC = "anthropic"
    ANTHROPIC_COMPATIBLE = "anthropic-compatible"
    OPENAI = "openai"
    OPENAI_COMPATIBLE = "openai-compatible"


KINDS_WITH_BASE_URL: frozenset[ProviderKind] = frozenset(
    {ProviderKind.ANTHROPIC_COMPATIBLE, ProviderKind.OPENAI_COMPATIBLE}
)
_KINDS_WITH_BASE_URL_NAMED = ", ".join(sorted(kind.value for kind in KINDS_WITH_BASE_URL))


@dataclass(frozen=True, slots=True)
class ModelProviderConfig:
    """One vendor, or one endpoint speaking a vendor's protocol, and where its key is read."""

    id: str
    kind: ProviderKind
    api_key_env: str
    base_url: str | None = None

    def __post_init__(self) -> None:
        checked_config_id(self.id, "a model provider's id")
        if not isinstance(self.kind, ProviderKind):
            raise InvalidValueError(
                f"a model provider's kind is a ProviderKind, not {describe(self.kind)}"
            )
        checked_line(self.api_key_env, "a model provider's api_key_env", MAX_ENV_NAME_CHARS)
        if not is_env_name(self.api_key_env):
            raise InvalidValueError(
                f"api_key_env is the NAME of an environment variable holding the key,"
                f" not {describe(self.api_key_env)}"
            )
        if self.kind in KINDS_WITH_BASE_URL:
            if not self.base_url:
                raise InvalidValueError(
                    f"a provider of kind {self.kind.value} needs its base_url: there"
                    f" is no endpoint to guess"
                )
            checked_line(self.base_url, "a model provider's base_url", MAX_BASE_URL_CHARS)
            if not is_endpoint_url(self.base_url):
                raise InvalidValueError(
                    "base_url is an https:// endpoint (http:// only on the loopback"
                    " interface), with no query, no fragment and no user:password in it"
                )
        elif self.base_url is not None:
            raise InvalidValueError(
                f"only these kinds have a base_url: {_KINDS_WITH_BASE_URL_NAMED};"
                f" {self.kind.value} has one endpoint of its own"
            )


MAX_MODEL_NAME_CHARS = 200
MAX_TITLE_CHARS = 120
DEFAULT_MODEL_TIMEOUT_SECONDS = 120.0
MAX_MODEL_TIMEOUT_SECONDS = 3600.0
MAX_OUTPUT_TOKENS = 10_000_000
MAX_CONTEXT_WINDOW = 100_000_000


@dataclass(frozen=True, slots=True)
class ModelConfig:
    """One model a turn may run on: whose it is, what the vendor calls it, its bounds."""

    id: str
    provider: str
    name: str
    timeout_seconds: float = DEFAULT_MODEL_TIMEOUT_SECONDS
    max_output_tokens: int | None = None
    context_window: int | None = None
    title: str = ""

    def __post_init__(self) -> None:
        checked_config_id(self.id, "a model's id")
        checked_config_id(self.provider, "a model provider's id")
        checked_line(self.name, "a model's name", MAX_MODEL_NAME_CHARS)
        if not self.name.strip():
            raise InvalidValueError("a model has a name: what the vendor calls it")
        checked_line(self.title, "a model's title", MAX_TITLE_CHARS)
        if not self.title:
            object.__setattr__(self, "title", self.id)
        elif not self.title.strip():
            raise InvalidValueError("a model's title is what a person picks it by, not spaces")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, int | float)
            or not 0 < self.timeout_seconds <= MAX_MODEL_TIMEOUT_SECONDS
        ):
            raise InvalidValueError(
                f"a model's timeout_seconds is a number of seconds over 0 and at most"
                f" {MAX_MODEL_TIMEOUT_SECONDS:g}, not {describe(self.timeout_seconds)}"
            )
        object.__setattr__(self, "timeout_seconds", float(self.timeout_seconds))
        if self.max_output_tokens is not None and (
            isinstance(self.max_output_tokens, bool)
            or not isinstance(self.max_output_tokens, int)
            or not 0 < self.max_output_tokens <= MAX_OUTPUT_TOKENS
        ):
            raise InvalidValueError(
                f"a model's max_output_tokens is a whole number over 0 and at most"
                f" {MAX_OUTPUT_TOKENS}, not {describe(self.max_output_tokens)}"
            )
        if self.context_window is not None and (
            isinstance(self.context_window, bool)
            or not isinstance(self.context_window, int)
            or not 0 < self.context_window <= MAX_CONTEXT_WINDOW
        ):
            raise InvalidValueError(
                f"a model's context_window is a whole number of tokens over 0 and at most"
                f" {MAX_CONTEXT_WINDOW}, not {describe(self.context_window)}"
            )


# --- tool servers -------------------------------------------------------------


class ToolServerAuth(StrEnum):
    BEARER = "bearer"
    BASIC = "basic"
    NONE = "none"


def _sends_alone(auth: ToolServerAuth) -> str:
    return (
        "none sends no credential"
        if auth is ToolServerAuth.NONE
        else f"{auth.value} sends the secret alone"
    )


DEFAULT_TOOL_TIMEOUT_SECONDS = 60.0
MAX_TOOL_TIMEOUT_SECONDS = MAX_MODEL_TIMEOUT_SECONDS
MAX_BASIC_USER_CHARS = 200


@dataclass(frozen=True, slots=True)
class ToolServerConfig:
    """One remote MCP server, and how it is reached and credentialed."""

    id: str
    url: str
    secret_env: str = ""
    auth: ToolServerAuth = ToolServerAuth.BEARER
    user: str = ""
    timeout_seconds: float = DEFAULT_TOOL_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        checked_config_id(self.id, "a tool server's id")
        checked_line(self.url, "a tool server's url", MAX_BASE_URL_CHARS)
        if not is_endpoint_url(self.url):
            raise InvalidValueError(
                "a tool server's url is an https:// endpoint (http:// only on the loopback"
                " interface), with no query, no fragment and no user:password in it"
            )
        if not isinstance(self.auth, ToolServerAuth):
            raise InvalidValueError(
                f"a tool server's auth is a ToolServerAuth, not {describe(self.auth)}"
            )
        if self.auth is ToolServerAuth.NONE:
            if not isinstance(self.secret_env, str) or self.secret_env:
                raise InvalidValueError(
                    'a tool server with auth = "none" names no secret_env: there is no secret'
                    " to read"
                )
        else:
            checked_line(self.secret_env, "a tool server's secret_env", MAX_ENV_NAME_CHARS)
            if not is_env_name(self.secret_env):
                raise InvalidValueError(
                    f"secret_env is the NAME of an environment variable holding the secret,"
                    f" not {describe(self.secret_env)}"
                )
        checked_line(self.user, "a tool server's user", MAX_BASIC_USER_CHARS)
        if self.auth is ToolServerAuth.BASIC:
            if not self.user.strip():
                raise InvalidValueError("a tool server with basic auth names the user part: user")
            if ":" in self.user:
                raise InvalidValueError("the user part of a basic credential holds no ':'")
        elif self.user:
            raise InvalidValueError(f"only basic auth has a user part; {_sends_alone(self.auth)}")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, int | float)
            or not 0 < self.timeout_seconds <= MAX_TOOL_TIMEOUT_SECONDS
        ):
            raise InvalidValueError(
                f"a tool server's timeout_seconds is a number of seconds over 0 and at most"
                f" {MAX_TOOL_TIMEOUT_SECONDS:g}, not {describe(self.timeout_seconds)}"
            )
        object.__setattr__(self, "timeout_seconds", float(self.timeout_seconds))


# --- the configuration an engine is built with --------------------------------


@dataclass(frozen=True, slots=True)
class ModelsConfig:
    """The providers, the models and the tool servers. No agents: those are the caller's."""

    providers: Mapping[str, ModelProviderConfig] = field(default_factory=dict)
    models: Mapping[str, ModelConfig] = field(default_factory=dict)
    tool_servers: Mapping[str, ToolServerConfig] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for where, table, wanted in (
            ("providers", self.providers, ModelProviderConfig),
            ("models", self.models, ModelConfig),
            ("tool_servers", self.tool_servers, ToolServerConfig),
        ):
            for key, value in table.items():
                if not isinstance(value, wanted) or value.id != key:
                    raise InvalidValueError(f"the entry under {where}.{key} is not that entry")
            object.__setattr__(self, where, MappingProxyType(dict(table)))
        for model in self.models.values():
            if model.provider not in self.providers:
                raise InvalidValueError(
                    f"model {model.id!r} is reached through provider {model.provider!r},"
                    f" which is not configured"
                )

    def model_of(self, model_id: str) -> ModelConfig:
        """The model of that id; ``UnknownModelError`` if there is none."""
        checked_config_id(model_id, "a model's id")
        found = self.models.get(model_id)
        if found is None:
            raise UnknownModelError(f"no model {model_id!r} is configured")
        return found

    def provider_of(self, model: ModelConfig) -> ModelProviderConfig:
        return self.providers[model.provider]


# --- the agent, as an engine sees it -----------------------------------------

MAX_SYSTEM_PROMPT_CHARS = 100_000


@dataclass(frozen=True, slots=True)
class AgentDefinition:
    """The system prompt and the tool servers an agent may use. Passed at every turn."""

    system_prompt: str
    tools: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        checked_text(self.system_prompt, "an agent's system prompt", MAX_SYSTEM_PROMPT_CHARS)
        if not isinstance(self.tools, tuple):
            raise InvalidValueError(
                f"an agent's tools are a tuple of ids, not {describe(self.tools)}"
            )
        for server_id in self.tools:
            checked_config_id(server_id, "a tool server's id")
        if len(set(self.tools)) != len(self.tools):
            raise InvalidValueError("an agent names each tool server once")


# --- the events of a turn -----------------------------------------------------

MAX_CHECKPOINT_ID_CHARS = 256


@dataclass(frozen=True, slots=True)
class TextDelta:
    text: str

    def __post_init__(self) -> None:
        checked_fragment(self.text, "a delta's text", MAX_PART_CHARS)


@dataclass(frozen=True, slots=True)
class ReasoningDelta:
    text: str

    def __post_init__(self) -> None:
        checked_fragment(self.text, "a delta's text", MAX_PART_CHARS)


@dataclass(frozen=True, slots=True)
class ToolCall:
    call_id: str
    name: str
    arguments: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        checked_call_id(self.call_id, "a tool call's id")
        checked_tool_name(self.name, "a tool call's name")
        object.__setattr__(
            self, "arguments", checked_data(self.arguments, "a tool call's arguments")
        )


@dataclass(frozen=True, slots=True)
class ToolResult:
    call_id: str
    name: str
    output: str
    is_error: bool = False

    def __post_init__(self) -> None:
        checked_call_id(self.call_id, "a tool call's id")
        checked_tool_name(self.name, "a tool's name")
        checked_fragment(self.output, "a tool result's output", MAX_PART_CHARS)
        if not isinstance(self.is_error, bool):
            raise InvalidValueError(
                f"whether a tool result is an error is yes or no, not {describe(self.is_error)}"
            )


@dataclass(frozen=True, slots=True)
class Done:
    """The final answer's text, and the engine's id for the memory after this turn."""

    text: str
    checkpoint_id: str

    def __post_init__(self) -> None:
        checked_fragment(self.text, "an answer's text", MAX_PART_CHARS)
        if not isinstance(self.checkpoint_id, str) or not self.checkpoint_id:
            raise InvalidValueError(
                f"a checkpoint id is non-empty text, not {describe(self.checkpoint_id)}"
            )
        checked_line(self.checkpoint_id, "a checkpoint id", MAX_CHECKPOINT_ID_CHARS)


Event = TextDelta | ReasoningDelta | ToolCall | ToolResult | Done
