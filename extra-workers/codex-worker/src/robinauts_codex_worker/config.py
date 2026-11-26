# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""What Codex is told on its command line, as ``-c key=value`` overrides of its configuration,
so that no configuration file of Codex's is read or written: the model providers, the agent's
tool servers, and everything that would phone home switched off.

Pure: it is handed the engine settings and gives back the arguments, and the environment the
secrets go in. A secret is never on the command line, where any process could read it: a
provider's key is read by Codex from the variable its ``env_key`` names, and a tool server's
header from a variable its ``env_http_headers`` names, both set in Codex's environment alone.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from robinauts.agent_engines.contract.domain import (
    AgentDefinition,
    ModelProviderConfig,
    ModelsConfig,
    ProviderKind,
)
from robinauts.agent_engines.contract.ports import ToolSecretLookup
from robinauts_codex_worker.settings import AuthMode
from robinauts_worker_common.tool_servers import headers_for

OPENAI_URL = "https://api.openai.com/v1"
OPENAI = "openai"
"""Codex's own provider, the one a ChatGPT login reaches."""
PROVIDER_PREFIX = "robinauts_"
"""What this worker's providers are named after in Codex: never one of Codex's own."""
HEADER_VARIABLE = "ROBINAUTS_CODEX_TOOL_HEADER_{index}"

QUIET: Mapping[str, Any] = {
    "analytics.enabled": False,
    "feedback.enabled": False,
    "otel.exporter": "none",
    "otel.trace_exporter": "none",
    "otel.metrics_exporter": "none",
    "check_for_update_on_startup": False,
    "history.persistence": "none",
    # The tool that waits for a person's answer: there is none at the other end.
    "tools.experimental_request_user_input.enabled": False,
}
"""Nothing but the turns leaves the container (docs/specs/core.md, "Nothing phones home")."""


def toml(value: Any) -> str:
    """A value as TOML, inline: what a ``-c`` override parses. A string is written as JSON
    writes it, which TOML reads as the same basic string."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return json.dumps(value)
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, Mapping):
        return "{" + ", ".join(f"{json.dumps(str(k))} = {toml(v)}" for k, v in value.items()) + "}"
    if isinstance(value, Sequence):
        return "[" + ", ".join(toml(item) for item in value) + "]"
    raise TypeError(f"no TOML for {type(value).__name__}")


def provider_id(mode: AuthMode, provider: ModelProviderConfig) -> str:
    """The provider's name in Codex: its own ``openai`` for a ChatGPT login, else one of ours."""
    if mode is AuthMode.PERSONAL:
        return OPENAI
    return PROVIDER_PREFIX + provider.id


def model_providers(mode: AuthMode, models: ModelsConfig) -> dict[str, dict[str, Any]]:
    """Codex's ``model_providers``: one per configured provider it can reach, each with the
    variable its key is read from. None in personal mode, which runs on Codex's own."""
    if mode is AuthMode.PERSONAL:
        return {}
    found: dict[str, dict[str, Any]] = {}
    for provider in models.providers.values():
        if provider.kind not in (ProviderKind.OPENAI, ProviderKind.OPENAI_COMPATIBLE):
            continue
        found[provider_id(mode, provider)] = {
            "name": provider.id,
            "base_url": provider.base_url or OPENAI_URL,
            "env_key": provider.api_key_env,
            "wire_api": "responses",
            "requires_openai_auth": False,
        }
    return found


def mcp_servers(
    agent: AgentDefinition, models: ModelsConfig, secrets: ToolSecretLookup
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    """Codex's ``mcp_servers`` for the agent's tool servers, over streamable HTTP, and the
    environment their headers are read from. Each tool runs without asking: nobody can answer
    from the chat, and the operator chose the server."""
    servers: dict[str, dict[str, Any]] = {}
    environment: dict[str, str] = {}
    for server_id in agent.tools:
        table = models.tool_servers[server_id]
        named: dict[str, str] = {}
        for header, value in headers_for(table, secrets).items():
            variable = HEADER_VARIABLE.format(index=len(environment))
            environment[variable] = value
            named[header] = variable
        server: dict[str, Any] = {
            "url": table.url,
            "default_tools_approval_mode": "approve",
            "tool_timeout_sec": table.timeout_seconds,
        }
        if named:
            server["env_http_headers"] = named
        if table.exclude:
            server["disabled_tools"] = list(table.exclude)
        servers[server_id] = server
    return servers, environment


def overrides(
    mode: AuthMode,
    agent: AgentDefinition,
    models: ModelsConfig,
    secrets: ToolSecretLookup,
) -> tuple[list[str], dict[str, str]]:
    """The ``-c`` arguments of ``codex app-server``, and the environment beside them."""
    settings: dict[str, Any] = dict(QUIET)
    if mode is AuthMode.PERSONAL:
        settings["forced_login_method"] = "chatgpt"
    providers = model_providers(mode, models)
    if providers:
        settings["model_providers"] = providers
    servers, environment = mcp_servers(agent, models, secrets)
    settings["mcp_servers"] = servers
    arguments: list[str] = []
    for key, value in settings.items():
        arguments += ["-c", f"{key}={toml(value)}"]
    return arguments, environment
