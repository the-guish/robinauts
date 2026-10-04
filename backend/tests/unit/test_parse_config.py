# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

from __future__ import annotations

import pytest

from robinauts.controller.contract.domain import ConfigError, ProviderKind, ToolServerAuth
from robinauts.controller.core.config import parse_config

AGENT = {"title": "A", "system_prompt": "", "model": "fast"}


def test_builds_the_config() -> None:
    config = parse_config(
        {
            "model_providers": {"p": {"kind": "anthropic", "api_key_env": "P_KEY"}},
            "models": {
                "fast": {"provider": "p", "name": "fast-1", "title": "Fast"},
                "slow": {"provider": "p", "name": "slow-1", "max_output_tokens": 1000},
            },
            "tool_servers": {"t": {"url": "https://t", "auth": "none"}},
            "agents": {
                "a": {**AGENT, "engine": "langchain", "tools": ["t"]},
                "b": {**AGENT, "engine": "echo"},
            },
        }
    )
    assert config.providers["p"].kind is ProviderKind.ANTHROPIC
    assert config.models["slow"].max_output_tokens == 1000
    assert config.tool_servers["t"].auth is ToolServerAuth.NONE
    assert config.agents["a"].tools == ("t",)
    assert list(config.agents) == ["a", "b"]


def test_names_every_problem() -> None:
    with pytest.raises(ConfigError) as raised:
        parse_config(
            {
                "model_providers": {"p": {"kind": "anthropic", "api_key_env": "K", "colour": 1}},
                "models": {"fast": {"provider": "q", "name": "fast-1"}},
                "agents": {"a": {**AGENT, "engine": "langgraph"}},
            }
        )
    message = str(raised.value)
    assert "model_providers.p: unknown key(s) colour" in message
    assert "models.fast: provider 'q'" in message
    assert "agents.a: engine 'langgraph'" in message


def test_a_header_server_names_its_header() -> None:
    config = parse_config(
        {
            "tool_servers": {
                "composio": {
                    "url": "https://backend.composio.dev/v3/mcp/abc/mcp?user_id=ops",
                    "auth": "header",
                    "header": "x-api-key",
                    "secret_env": "COMPOSIO_API_KEY",
                }
            }
        }
    )
    server = config.tool_servers["composio"]
    assert (server.auth, server.header) == (ToolServerAuth.HEADER, "x-api-key")


def test_a_header_server_without_a_header_or_with_a_bad_one_is_refused() -> None:
    with pytest.raises(ConfigError) as raised:
        parse_config(
            {
                "tool_servers": {
                    "none": {"url": "https://t", "auth": "header"},
                    "bad": {"url": "https://t", "auth": "header", "header": "x-api-key: 1"},
                    "odd": {"url": "https://t", "auth": "header", "header": 7},
                }
            }
        )
    message = str(raised.value)
    assert 'tool_servers.none: auth "header" needs `header`' in message
    assert "tool_servers.bad: header 'x-api-key: 1' is not an HTTP header name" in message
    assert "tool_servers.odd: header 7 is not an HTTP header name" in message


def test_a_header_on_another_auth_is_refused() -> None:
    with pytest.raises(ConfigError) as raised:
        parse_config(
            {
                "tool_servers": {
                    "gh": {"url": "https://t", "header": "x-api-key"},
                    "wiki": {"url": "https://t", "auth": "basic", "header": "x-api-key"},
                }
            }
        )
    message = str(raised.value)
    assert "tool_servers.gh: `header` is for auth \"header\" alone, not auth 'bearer'" in message
    assert "tool_servers.wiki: `header` is for auth \"header\" alone, not auth 'basic'" in message
