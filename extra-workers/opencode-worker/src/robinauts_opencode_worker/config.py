# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""What OpenCode is configured with, as the JSON of ``OPENCODE_CONFIG_CONTENT``, so that no
configuration file of OpenCode's is read or written: the model providers with their keys, every
configured tool server, one agent of this worker's, and everything that would ask a person or
phone home switched off.

Pure: it is handed the engine settings and gives back the configuration, and the permission
rules a session runs with, so it is tested without an OpenCode process. The configuration holds
the keys and the tool servers' credentials: it goes in OpenCode's environment alone, never in a
file or a log.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from robinauts.agent_engines.contract.domain import (
    DEFAULT_CONTEXT_WINDOW,
    MissingSecretError,
    ModelConfig,
    ModelProviderConfig,
    ModelsConfig,
    ProviderKind,
)
from robinauts.agent_engines.contract.ports import ProviderKeyLookup, ToolSecretLookup
from robinauts_worker_common.tool_servers import headers_for

AGENT = "robinauts"
"""OpenCode's agent every turn runs as: its default prompt, with the agent's appended."""
FUNCTIONS_SERVER = "robinauts"
"""The MCP server of the tools run in the worker's process, added to each turn's directory."""
PROVIDER_PREFIX = "robinauts-"
"""What this worker's providers are named after in OpenCode: never one of OpenCode's own."""
DEFAULT_OUTPUT_TOKENS = 16_384
"""What a model may write in one answer when its table names no ``max_output_tokens``."""

PACKAGES = {
    ProviderKind.ANTHROPIC: "@ai-sdk/anthropic",
    ProviderKind.ANTHROPIC_COMPATIBLE: "@ai-sdk/anthropic",
    ProviderKind.OPENAI: "@ai-sdk/openai",
    ProviderKind.OPENAI_COMPATIBLE: "@ai-sdk/openai-compatible",
}
"""The AI SDK's package for each provider kind: all bundled in OpenCode's binary, none fetched."""

ALLOW_ALL: Mapping[str, str] = {
    # The last rule that matches wins, so the catch-all goes first.
    "*": "allow",
    "external_directory": "allow",
    "doom_loop": "allow",
    # The tool that waits for a person's answer: there is none at the other end.
    "question": "deny",
}
"""Nothing waits for a person: every tool runs, but the one that asks."""

_UNSAFE = re.compile(r"[^a-zA-Z0-9_-]")


def tool_key(server_id: str) -> str:
    """The prefix of a tool server's tools in OpenCode: its id, made safe, and ``_``."""
    return _UNSAFE.sub("_", server_id) + "_"


def provider_id(provider: ModelProviderConfig) -> str:
    return PROVIDER_PREFIX + provider.id


def base_url(provider: ModelProviderConfig) -> str | None:
    """The endpoint OpenCode's client takes: the AI SDK's Anthropic client wants the ``/v1``
    that the Anthropic SDK adds on its own."""
    url = provider.base_url
    if url is None:
        return None
    url = url.rstrip("/")
    if provider.kind in (ProviderKind.ANTHROPIC, ProviderKind.ANTHROPIC_COMPATIBLE):
        if not url.endswith("/v1"):
            url += "/v1"
    return url


def _model(model: ModelConfig) -> dict[str, Any]:
    return {
        "name": model.title or model.name,
        "tool_call": True,
        "limit": {
            "context": model.context_window or DEFAULT_CONTEXT_WINDOW,
            "output": model.max_output_tokens or DEFAULT_OUTPUT_TOKENS,
        },
    }


def providers(models: ModelsConfig, keys: ProviderKeyLookup) -> dict[str, dict[str, Any]]:
    """OpenCode's ``provider`` table: each configured provider this process has a key for, with
    its models by the name the vendor knows them by."""
    found: dict[str, dict[str, Any]] = {}
    for provider in models.providers.values():
        try:
            key = keys.key_for(provider.id)
        except MissingSecretError:
            continue
        options: dict[str, Any] = {"apiKey": key}
        if (url := base_url(provider)) is not None:
            options["baseURL"] = url
        found[provider_id(provider)] = {
            "npm": PACKAGES[provider.kind],
            "name": provider.id,
            "options": options,
            "models": {
                model.name: _model(model)
                for model in models.models.values()
                if model.provider == provider.id
            },
        }
    return found


def mcp(models: ModelsConfig, secrets: ToolSecretLookup) -> dict[str, dict[str, Any]]:
    """OpenCode's ``mcp`` table: every configured tool server, over HTTP with its header. Which
    of them a turn's agent may use is the session's permission rules (``tool_rules``)."""
    servers: dict[str, dict[str, Any]] = {}
    for server_id, table in models.tool_servers.items():
        try:
            headers = headers_for(table, secrets)
        except MissingSecretError:
            continue
        servers[server_id] = {
            "type": "remote",
            "url": table.url,
            "headers": headers,
            "enabled": True,
            "oauth": False,
            "timeout": int(table.timeout_seconds * 1000),
        }
    return servers


def configuration(
    models: ModelsConfig,
    keys: ProviderKeyLookup,
    secrets: ToolSecretLookup,
    *,
    max_steps: int | None = None,
) -> dict[str, Any]:
    """The whole of ``OPENCODE_CONFIG_CONTENT``."""
    found = providers(models, keys)
    agent: dict[str, Any] = {
        "mode": "primary",
        "description": "The agents of a Robinauts worker",
        "permission": dict(ALLOW_ALL),
    }
    if max_steps is not None:
        agent["steps"] = max_steps
    return {
        "$schema": "https://opencode.ai/config.json",
        "share": "disabled",
        "autoupdate": False,
        "snapshot": False,
        "enabled_providers": sorted(found),
        "provider": found,
        "mcp": mcp(models, secrets),
        "permission": dict(ALLOW_ALL),
        "agent": {AGENT: agent},
        "default_agent": AGENT,
    }


def tool_rules(
    allowed: tuple[str, ...], models: ModelsConfig, functions: bool
) -> list[dict[str, str]]:
    """A session's permission rules for a turn: the agent's tool servers, whole, and the tools
    run here when it has any; every other server's tools denied, and every tool an ``exclude``
    leaves out. Appended to the session's rules at every turn, and the last that matches wins,
    so the turn's rules are the ones in force."""
    rules: list[dict[str, str]] = []
    for server_id in models.tool_servers:
        action = "allow" if server_id in allowed else "deny"
        rules.append({"permission": tool_key(server_id) + "*", "pattern": "*", "action": action})
    for server_id in allowed:
        for name in models.tool_servers[server_id].exclude:
            rules.append(
                {
                    "permission": tool_key(server_id) + _UNSAFE.sub("_", name),
                    "pattern": "*",
                    "action": "deny",
                }
            )
    rules.append(
        {
            "permission": tool_key(FUNCTIONS_SERVER) + "*",
            "pattern": "*",
            "action": "allow" if functions else "deny",
        }
    )
    return rules


def tool_name(name: str, models: ModelsConfig) -> str:
    """A tool's name as the chat shows it: an MCP tool's without the server's prefix OpenCode
    gives it, as the other engines name them; OpenCode's own as they are."""
    for server_id in (FUNCTIONS_SERVER, *models.tool_servers):
        prefix = tool_key(server_id)
        if name.startswith(prefix) and len(name) > len(prefix):
            return name[len(prefix) :]
    return name
