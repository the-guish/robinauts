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
    ModelConfig,
    ProviderConfig,
    ProviderKind,
    ToolServerAuth,
    ToolServerConfig,
    WorkConfig,
)

ENGINES = ("langchain", "pydantic-ai", "echo")

# An HTTP field name is a token (RFC 9110, section 5.1): nothing else can go on the wire.
HEADER_NAME = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")


def _names(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(name, str) for name in value):
        raise ValueError(f"exclude is not a list of tool names: {value!r}")
    return tuple(value)


_MAX_TASKS = "max_running_tasks_per_worker"
"""The one ``[work]`` key that counts turns rather than seconds."""


def _positive(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and value > 0


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
    tool_servers = build("tool_servers", ToolServerConfig, auth=ToolServerAuth, exclude=_names)
    agents = build("agents", AgentConfig, tools=tuple)

    work = WorkConfig()
    given = dict(raw.get("work", {}))
    tasks = given.pop(_MAX_TASKS, work.max_running_tasks_per_worker)
    if unknown := sorted(set(given) - {field.name for field in dataclasses.fields(WorkConfig)}):
        problems.append(f"work: unknown key(s) {', '.join(unknown)}")
    elif wrong := sorted(key for key, value in given.items() if not _positive(value)):
        problems.append(f"work: {', '.join(wrong)} must be a positive number of seconds")
    elif not (isinstance(tasks, int) and not isinstance(tasks, bool) and tasks > 0):
        problems.append(f"work: {_MAX_TASKS} must be a positive whole number")
    else:
        seconds = {key: float(value) for key, value in given.items()}
        work = WorkConfig(**seconds, max_running_tasks_per_worker=tasks)
        if work.heartbeat_seconds * 2 > work.lease_seconds:
            problems.append("work: heartbeat_seconds must be at most half of lease_seconds")

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
        if twice := sorted({tool for tool in agent.tools if agent.tools.count(tool) > 1}):
            problems.append(f"agents.{agent.id}: tool server(s) named twice: {', '.join(twice)}")
        if agent.engine not in ENGINES:
            problems.append(f"agents.{agent.id}: engine {agent.engine!r} is not one of {ENGINES}")
    if problems:
        raise ConfigError("\n".join(problems))
    return Config(providers, models, tool_servers, agents, work)
