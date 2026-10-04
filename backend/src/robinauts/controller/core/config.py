# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

from __future__ import annotations

import dataclasses
import re
from collections.abc import Callable, Mapping
from typing import Any

from robinauts.controller.contract.domain import (
    AgentConfig,
    Config,
    ConfigError,
    DatabaseConfig,
    ModelConfig,
    ProviderConfig,
    ProviderKind,
    ToolErrorBehavior,
    ToolServerAuth,
    ToolServerConfig,
    WorkConfig,
)

ENGINES = ("langchain", "pydantic-ai", "echo")

# An HTTP field name is a token (RFC 9110, section 5.1): nothing else can go on the wire.
HEADER_NAME = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")


SHORTEST_TURN_SECONDS = 30.0
"""The least `[work] max_turn_seconds` may be: a runner needs some of a deadline to claim a
turn at all, and a turn shorter than this would mostly fail before its first word."""


def seconds(value: Any) -> float:
    """A number of seconds above zero."""
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        raise ValueError(f"{value!r} is not a number of seconds above zero")
    return float(value)


def count(value: Any) -> int:
    """A whole number above zero."""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{value!r} is not a whole number above zero")
    return value


def retries(value: Any) -> int:
    """A whole number, zero or more."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{value!r} is not a whole number, zero or more")
    return value


def settings_table(
    raw: Mapping[str, Any],
    table: str,
    cls: Any,
    problems: list[str],
    **convert: Callable[[Any], Any],
) -> Any:
    """A table of settings, such as ``[work]``: every key one of ``cls``'s fields, each
    checked by its converter; the defaults where the table, or a key, is left out."""
    fields = raw.get(table, {})
    if not isinstance(fields, Mapping):
        problems.append(f"{table}: a table of settings, not {fields!r}")
        return cls()
    known = {field.name for field in dataclasses.fields(cls)}
    if unknown := sorted(set(fields) - known):
        problems.append(f"{table}: unknown key(s) {', '.join(unknown)}")
    values = {}
    for key in sorted(set(fields) & known):
        try:
            values[key] = convert.get(key, lambda v: v)(fields[key])
        except (TypeError, ValueError) as error:
            problems.append(f"{table}.{key}: {error}")
    return cls(**values)


def parse_config(raw: Mapping[str, Any]) -> Config:
    problems: list[str] = []

    def build(table: str, cls: Any, **convert: Callable[[Any], Any]) -> dict[str, Any]:
        built = {}
        known = {field.name for field in dataclasses.fields(cls)} - {"id"}
        for id_, fields in raw.get(table, {}).items():
            if unknown := sorted(set(fields) - known):
                problems.append(f"{table}.{id_}: unknown key(s) {', '.join(unknown)}")
                continue
            try:
                values = {key: convert.get(key, lambda v: v)(v) for key, v in fields.items()}
                built[id_] = cls(id=id_, **values)
            except (TypeError, ValueError) as error:
                problems.append(f"{table}.{id_}: {error}")
        return built

    providers = build("model_providers", ProviderConfig, kind=ProviderKind)
    models = build("models", ModelConfig, timeout_seconds=seconds, max_retries=retries)
    tool_servers = build("tool_servers", ToolServerConfig, auth=ToolServerAuth)
    agents = build("agents", AgentConfig, tools=tuple)
    work = settings_table(
        raw,
        "work",
        WorkConfig,
        problems,
        max_turn_seconds=seconds,
        lease_seconds=seconds,
        heartbeat_seconds=seconds,
        drain_seconds=seconds,
        max_model_calls=count,
        tool_error_behavior=ToolErrorBehavior,
    )
    database = settings_table(
        raw,
        "database",
        DatabaseConfig,
        problems,
        pool_max=count,
        acquire_timeout_seconds=seconds,
    )
    if work.max_turn_seconds < SHORTEST_TURN_SECONDS:
        problems.append(
            f"work: max_turn_seconds {work.max_turn_seconds:g} is shorter than a turn can be"
            f" ({SHORTEST_TURN_SECONDS:g} s at least)"
        )
    if work.heartbeat_seconds * 2 > work.lease_seconds:
        problems.append(
            f"work: heartbeat_seconds {work.heartbeat_seconds:g} is more than half of"
            f" lease_seconds {work.lease_seconds:g}, so one late heartbeat would lose every turn"
        )

    for server in tool_servers.values():
        if server.auth is ToolServerAuth.HEADER:
            if not server.header:
                problems.append(
                    f'tool_servers.{server.id}: auth "header" needs `header`, '
                    "the name of the header the secret is sent in"
                )
            elif not isinstance(server.header, str) or not HEADER_NAME.fullmatch(server.header):
                problems.append(
                    f"tool_servers.{server.id}: header {server.header!r} is not an HTTP header name"
                )
        elif server.header:
            problems.append(
                f'tool_servers.{server.id}: `header` is for auth "header" alone, '
                f"not auth {server.auth.value!r}"
            )
    for model in models.values():
        if model.provider not in providers:
            problems.append(f"models.{model.id}: provider {model.provider!r} is not configured")
    for agent in agents.values():
        if agent.model not in models:
            problems.append(f"agents.{agent.id}: model {agent.model!r} is not configured")
        for tool in agent.tools:
            if tool not in tool_servers:
                problems.append(f"agents.{agent.id}: tool server {tool!r} is not configured")
        if agent.engine not in ENGINES:
            problems.append(f"agents.{agent.id}: engine {agent.engine!r} is not one of {ENGINES}")
    if problems:
        raise ConfigError("\n".join(problems))
    return Config(providers, models, tool_servers, agents, work=work, database=database)
