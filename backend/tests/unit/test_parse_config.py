# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

from __future__ import annotations

import re

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


@pytest.mark.parametrize(
    ("exclude", "parsed", "problem"),
    [
        ([], (), None),
        (["COMPOSIO_MANAGE_SKILL"], ("COMPOSIO_MANAGE_SKILL",), None),
        (["a", "b"], ("a", "b"), None),
        ("a", None, "exclude is not a list of tool names: 'a'"),
        (["a", 1], None, "exclude is not a list of tool names: ['a', 1]"),
    ],
)
def test_a_tool_server_excludes_tools_by_name(
    exclude: object, parsed: tuple[str, ...] | None, problem: str | None
) -> None:
    raw = {"tool_servers": {"t": {"url": "https://t", "auth": "none", "exclude": exclude}}}
    if problem is None:
        assert parse_config(raw).tool_servers["t"].exclude == parsed
    else:
        with pytest.raises(ConfigError, match=re.escape(f"tool_servers.t: {problem}")):
            parse_config(raw)


def test_an_agent_naming_a_tool_server_twice_is_refused() -> None:
    raw = {
        "model_providers": {"p": {"kind": "anthropic", "api_key_env": "K"}},
        "models": {"fast": {"provider": "p", "name": "fast-1"}},
        "tool_servers": {"t": {"url": "https://t", "auth": "none"}},
        "agents": {"a": {**AGENT, "engine": "echo", "tools": ["t", "t"]}},
    }
    with pytest.raises(ConfigError, match=re.escape("agents.a: tool server(s) named twice: t")):
        parse_config(raw)


@pytest.mark.parametrize(
    ("given", "parsed", "problem"),
    [
        (None, 100, None),
        (1, 1, None),
        (250, 250, None),
        (0, None, "must be a positive whole number"),
        (-1, None, "must be a positive whole number"),
        (1.5, None, "must be a positive whole number"),
        (True, None, "must be a positive whole number"),
        ("4", None, "must be a positive whole number"),
    ],
)
def test_a_worker_runs_at_most_max_running_tasks_per_worker(
    given: object, parsed: int | None, problem: str | None
) -> None:
    raw = {"work": {} if given is None else {"max_running_tasks_per_worker": given}}
    if problem is None:
        assert parse_config(raw).work.max_running_tasks_per_worker == parsed
    else:
        with pytest.raises(ConfigError, match=f"work: max_running_tasks_per_worker {problem}"):
            parse_config(raw)
