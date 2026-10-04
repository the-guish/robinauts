# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

from __future__ import annotations

import pytest

from robinauts.controller.contract.domain import (
    ConfigError,
    ProviderKind,
    ToolErrorBehavior,
    ToolServerAuth,
    WorkConfig,
)
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


def test_work_takes_its_defaults_when_left_out() -> None:
    assert parse_config({}).work == WorkConfig()
    assert WorkConfig().max_turn_seconds == 1200.0
    assert WorkConfig().tool_error_behavior is ToolErrorBehavior.FAILED


def test_work_and_a_models_retries_are_read() -> None:
    config = parse_config(
        {
            "model_providers": {"p": {"kind": "anthropic", "api_key_env": "P_KEY"}},
            "models": {"m": {"provider": "p", "name": "m-1", "timeout_seconds": 60}},
            "work": {
                "max_turn_seconds": 3600,
                "max_model_calls": 250,
                "tool_error_behavior": "error",
            },
        }
    )
    assert config.work == WorkConfig(3600.0, 250, ToolErrorBehavior.ERROR)
    assert (config.models["m"].timeout_seconds, config.models["m"].max_retries) == (60.0, 2)


def test_work_and_retries_out_of_range_are_refused_by_name() -> None:
    with pytest.raises(ConfigError) as raised:
        parse_config(
            {
                "model_providers": {"p": {"kind": "anthropic", "api_key_env": "P_KEY"}},
                "models": {"m": {"provider": "p", "name": "m-1", "max_retries": -1}},
                "work": {
                    "max_turn_seconds": 0,
                    "max_model_calls": 2.5,
                    "tool_error_behavior": "ignore",
                    "lease": 90,
                },
            }
        )
    message = str(raised.value)
    assert "models.m: -1 is not a whole number, zero or more" in message
    assert "work: unknown key(s) lease" in message
    assert "work.max_turn_seconds: 0 is not a number of seconds above zero" in message
    assert "work.max_model_calls: 2.5 is not a whole number above zero" in message
    assert "work.tool_error_behavior: 'ignore' is not a valid ToolErrorBehavior" in message


def test_work_must_be_a_table() -> None:
    with pytest.raises(ConfigError, match="work: a table of settings"):
        parse_config({"work": 3})
