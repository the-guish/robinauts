# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

from __future__ import annotations

import dataclasses
import re
from collections.abc import Callable, Collection, Mapping
from typing import Any

from robinauts.controller.contract.domain import ConfigError
from robinauts.shared_domain.domain import (
    AgentConfig,
    Config,
    FilesConfig,
    FilesKind,
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

# A worker's name goes into a log line, a file name and the process's environment.
WORKER_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}")


def _worker(value: Any) -> str:
    if not isinstance(value, str) or not WORKER_NAME.fullmatch(value):
        raise ValueError(
            f"worker {value!r} is not a worker name: lowercase letters, digits, '-' and '_',"
            " starting with a letter or a digit, at most 63 characters"
        )
    return value


def _names(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(name, str) for name in value):
        raise ValueError(f"exclude is not a list of tool names: {value!r}")
    return tuple(value)


_MAX_TASKS = "max_running_tasks_per_worker"
"""The one ``[work]`` key that counts turns rather than seconds."""


def _positive(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and value > 0


def _seconds(value: Any) -> float:
    if not _positive(value):
        raise ValueError(f"turn_timeout_seconds is not a positive number of seconds: {value!r}")
    return float(value)


def _files(given: Mapping[str, Any], problems: list[str]) -> FilesConfig:
    """``[files]``, with its defaults for what is left out."""
    default = FilesConfig()
    if unknown := sorted(set(given) - {field.name for field in dataclasses.fields(FilesConfig)}):
        problems.append(f"files: unknown key(s) {', '.join(unknown)}")
        return default
    kind = given.get("kind", default.kind.value)
    path = given.get("path", default.path)
    wrong = False
    if not isinstance(kind, str) or kind not in {k.value for k in FilesKind}:
        kinds = ", ".join(k.value for k in FilesKind)
        problems.append(f"files: kind {kind!r} is not one of: {kinds}")
        wrong = True
    if not isinstance(path, str) or not path.strip():
        problems.append('files: path must name a folder, such as "~/robinauts"')
        wrong = True
    return default if wrong else FilesConfig(FilesKind(kind), path)


def parse_config(raw: Mapping[str, Any], tools: Collection[str] = ()) -> Config:
    """``tools`` are the ids of the tools this build runs in process, which an agent's ``tools``
    may name beside the tool servers."""
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
    agents = build(
        "agents", AgentConfig, tools=tuple, turn_timeout_seconds=_seconds, worker=_worker
    )

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
    files = _files(dict(raw.get("files", {})), problems)

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
    for server_id in sorted(set(tool_servers) & set(tools)):
        problems.append(f"tool_servers.{server_id}: the id of a tool of this build")
    for model in models.values():
        if model.provider not in providers:
            problems.append(f"models.{model.id}: provider {model.provider!r} is not configured")
    for agent in agents.values():
        if agent.model not in models:
            problems.append(f"agents.{agent.id}: model {agent.model!r} is not configured")
        for tool in agent.tools:
            if tool not in tool_servers and tool not in tools:
                problems.append(
                    f"agents.{agent.id}: tool {tool!r} is neither a configured tool server"
                    " nor a tool of this build"
                )
        if twice := sorted({tool for tool in agent.tools if agent.tools.count(tool) > 1}):
            problems.append(f"agents.{agent.id}: tool(s) named twice: {', '.join(twice)}")
        if agent.engine not in ENGINES:
            problems.append(f"agents.{agent.id}: engine {agent.engine!r} is not one of {ENGINES}")
    if problems:
        raise ConfigError("\n".join(problems))
    return Config(providers, models, tool_servers, agents, work, files)
