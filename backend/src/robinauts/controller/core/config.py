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
    ToolServerAuth,
    ToolServerConfig,
    WorkConfig,
)

ENGINES = ("langchain", "pydantic-ai", "echo")

TABLES = ("model_providers", "models", "tool_servers", "agents", "work", "database")
"""The file's tables that are the controller's."""

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

    def single(table: str, cls: Any) -> Any:
        """A table of settings, not of ids: every key known, every value a positive number of
        the field's type."""
        fields = raw.get(table, {})
        types = {f.name: f.type for f in dataclasses.fields(cls)}
        values = {}
        for key, value in fields.items():
            if key not in types:
                problems.append(f"{table}: unknown key {key}")
            elif not _positive(value, integer=types[key] == "int"):
                kind = "a whole number" if types[key] == "int" else "a number"
                problems.append(f"{table}.{key}: {value!r} is not {kind} above 0")
            else:
                values[key] = value
        return cls(**values)

    providers = build("model_providers", ProviderConfig, kind=ProviderKind)
    models = build("models", ModelConfig)
    tool_servers = build("tool_servers", ToolServerConfig, auth=ToolServerAuth)
    agents = build("agents", AgentConfig, tools=tuple)
    work = single("work", WorkConfig)
    database = single("database", DatabaseConfig)

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
            problems.append(f"models.{model.id}: max_retries {retries!r} is not a whole number")
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


def _positive(value: Any, *, integer: bool) -> bool:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return False
    return value > 0 and (not integer or isinstance(value, int))
