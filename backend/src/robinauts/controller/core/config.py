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
    ToolErrors,
    ToolServerAuth,
    ToolServerConfig,
    WorkConfig,
)

ENGINES = ("langchain", "pydantic-ai", "echo")

# An HTTP field name is a token (RFC 9110, section 5.1): nothing else can go on the wire.
HEADER_NAME = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")


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
    models = build("models", ModelConfig)
    tool_servers = build(
        "tool_servers", ToolServerConfig, auth=ToolServerAuth, tool_errors=ToolErrors
    )
    agents = build("agents", AgentConfig, tools=tuple)
    work = _numbers("work", WorkConfig, raw.get("work", {}), problems)
    database = _numbers("database", DatabaseConfig, raw.get("database", {}), problems)
    if database.pool_max < 2:
        problems.append("database: pool_max must be 2 or more")
    if work.heartbeat_seconds * 2 > work.lease_seconds:
        problems.append(
            "work: heartbeat_seconds must be at most half of lease_seconds, so that one late"
            " heartbeat does not lose a turn"
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
        retries = model.max_retries
        if isinstance(retries, bool) or not isinstance(retries, int) or retries < 0:
            problems.append(f"models.{model.id}: max_retries must be a whole number, 0 or more")
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
    return Config(providers, models, tool_servers, agents, work, database)


def _positive(table: str, key: str, value: Any, kind: type, problems: list[str]) -> bool:
    if isinstance(value, bool) or not isinstance(value, kind) or value <= 0:
        problems.append(f"{table}: {key} must be a number more than 0, not {value!r}")
        return False
    return True


def _numbers(table: str, cls: Any, raw: Mapping[str, Any], problems: list[str]) -> Any:
    """A single table of numbers, ``[work]`` or ``[database]``, each with its default."""
    if not isinstance(raw, Mapping):
        problems.append(f"{table}: a table of keys, not a value")
        return cls()
    fields = {f.name: f for f in dataclasses.fields(cls)}
    if unknown := sorted(set(raw) - set(fields)):
        problems.append(f"{table}: unknown key(s) {', '.join(unknown)}")
    values: dict[str, Any] = {}
    for key, value in raw.items():
        if key not in fields:
            continue
        kind = (int, float) if fields[key].type == "float" else int
        if _positive(table, key, value, kind, problems):
            values[key] = value
    return cls(**values)
