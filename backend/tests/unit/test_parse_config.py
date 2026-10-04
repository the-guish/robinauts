# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

from __future__ import annotations

import pytest

from robinauts.controller.contract.domain import (
    ConfigError,
    ProviderKind,
    ToolErrors,
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


def test_the_work_table_has_defaults_and_takes_numbers() -> None:
    assert parse_config({}).work == WorkConfig(max_turn_seconds=1200, max_model_calls=100)
    work = parse_config({"work": {"max_turn_seconds": 10800, "max_model_calls": 7}}).work
    assert (work.max_turn_seconds, work.max_model_calls) == (10800, 7)


def test_the_work_table_names_every_problem() -> None:
    with pytest.raises(ConfigError) as raised:
        parse_config({"work": {"max_turn_seconds": 0, "max_model_calls": 2.5, "pace": 1}})
    message = str(raised.value)
    assert "work: unknown key(s) pace" in message
    assert "work: max_turn_seconds must be a number more than 0" in message
    assert "work: max_model_calls must be a number more than 0, not 2.5" in message


def test_a_model_retries_twice_unless_told_otherwise() -> None:
    config = parse_config(
        {
            "model_providers": {"p": {"kind": "anthropic", "api_key_env": "P_KEY"}},
            "models": {
                "fast": {"provider": "p", "name": "fast-1"},
                "patient": {"provider": "p", "name": "slow-1", "max_retries": 5},
            },
        }
    )
    assert (config.models["fast"].max_retries, config.models["patient"].max_retries) == (2, 5)
    with pytest.raises(ConfigError, match="models.fast: max_retries must be a whole number"):
        parse_config(
            {
                "model_providers": {"p": {"kind": "anthropic", "api_key_env": "P_KEY"}},
                "models": {"fast": {"provider": "p", "name": "fast-1", "max_retries": -1}},
            }
        )


def test_a_tool_server_reports_its_errors_unless_told_to_retry() -> None:
    servers = parse_config(
        {
            "tool_servers": {
                "a": {"url": "https://a", "auth": "none"},
                "b": {"url": "https://b", "auth": "none", "tool_errors": "retry"},
            }
        }
    ).tool_servers
    assert (servers["a"].tool_errors, servers["b"].tool_errors) == (
        ToolErrors.REPORT,
        ToolErrors.RETRY,
    )
    with pytest.raises(ConfigError, match="tool_servers.a"):
        parse_config({"tool_servers": {"a": {"url": "https://a", "tool_errors": "ignore"}}})


def test_a_heartbeat_slower_than_half_the_lease_is_refused() -> None:
    assert parse_config({"work": {"lease_seconds": 60, "heartbeat_seconds": 30}}).work == (
        WorkConfig(lease_seconds=60, heartbeat_seconds=30)
    )
    with pytest.raises(ConfigError, match="heartbeat_seconds must be at most half"):
        parse_config({"work": {"lease_seconds": 90, "heartbeat_seconds": 60}})
