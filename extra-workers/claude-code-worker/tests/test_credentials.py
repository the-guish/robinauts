# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

from __future__ import annotations

import pytest

from robinauts.agent_engines.contract.domain import ModelProviderConfig, ProviderKind
from robinauts.agent_engines.contract.ports import ProviderKeyLookup
from robinauts_claude_worker.credentials import (
    credential_problems,
    is_local,
    refused_text,
    subprocess_environment,
)
from robinauts_claude_worker.settings import AuthMode

ANTHROPIC = ModelProviderConfig("anthropic", ProviderKind.ANTHROPIC, "ANTHROPIC_API_KEY")
GATEWAY = ModelProviderConfig(
    "gateway", ProviderKind.ANTHROPIC_COMPATIBLE, "GATEWAY_KEY", "https://gateway.example/api"
)
OPENAI = ModelProviderConfig("openai", ProviderKind.OPENAI, "OPENAI_API_KEY")
LOCAL = "http://127.0.0.1:8000"
TOKEN = {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-made-by-the-person"}


class Keys(ProviderKeyLookup):
    def key_for(self, provider_id: str) -> str:
        return f"key-of-{provider_id}"


@pytest.mark.parametrize(
    ("url", "local"),
    [
        ("http://127.0.0.1:8000", True),
        ("http://localhost:8000", True),
        ("http://[::1]:8000", True),
        ("http://host.docker.internal:8000", True),
        ("http://172.17.0.1:8000", True),
        ("http://192.168.1.20:8000", True),
        ("http://10.0.0.5:8000", True),
        ("unix:///run/robinauts/robinauts.sock", True),
        ("https://robinauts.example.com", False),
        ("http://backend:8000", False),
        ("http://8.8.8.8:8000", False),
    ],
)
def test_what_counts_as_the_persons_own_machine(url: str, local: bool) -> None:
    assert is_local(url) is local


def test_personal_mode_runs_with_the_token_alone_against_a_local_server() -> None:
    assert credential_problems(AuthMode.PERSONAL, TOKEN, LOCAL, [ANTHROPIC]) == []


@pytest.mark.parametrize(
    ("environ", "url", "providers", "problem"),
    [
        ({}, LOCAL, [ANTHROPIC], "personal mode needs CLAUDE_CODE_OAUTH_TOKEN"),
        ({**TOKEN, "ANTHROPIC_API_KEY": "k"}, LOCAL, [ANTHROPIC], "refuses ANTHROPIC_API_KEY"),
        (
            {**TOKEN, "ANTHROPIC_AUTH_TOKEN": "k"},
            LOCAL,
            [ANTHROPIC],
            "refuses ANTHROPIC_AUTH_TOKEN",
        ),
        ({**TOKEN, "CLAUDE_CODE_USE_BEDROCK": "1"}, LOCAL, [], "refuses CLAUDE_CODE_USE_BEDROCK"),
        (
            {**TOKEN, "GATEWAY_KEY": "k"},
            LOCAL,
            [GATEWAY],
            "refuses GATEWAY_KEY, the key of provider",
        ),
        ({**TOKEN}, LOCAL, [GATEWAY], "refuses provider 'gateway': a subscription token goes"),
        ({**TOKEN}, LOCAL, [OPENAI], "refuses provider 'openai'"),
        ({**TOKEN, "ANTHROPIC_BASE_URL": "https://x"}, LOCAL, [], "refuses ANTHROPIC_BASE_URL"),
        ({**TOKEN}, "https://robinauts.example.com", [ANTHROPIC], "is not on this machine"),
    ],
)
def test_personal_mode_refuses_what_would_take_the_token_elsewhere(
    environ: dict[str, str], url: str, providers: list[ModelProviderConfig], problem: str
) -> None:
    problems = credential_problems(AuthMode.PERSONAL, environ, url, providers)
    assert any(problem in line for line in problems), problems


def test_deployed_mode_runs_on_the_providers_keys() -> None:
    environ = {"ANTHROPIC_API_KEY": "k", "GATEWAY_KEY": "g"}
    url = "http://backend:8000"
    assert credential_problems(AuthMode.DEPLOYED, environ, url, [ANTHROPIC, GATEWAY]) == []


@pytest.mark.parametrize(
    ("environ", "providers", "problem"),
    [
        (
            {**TOKEN, "ANTHROPIC_API_KEY": "k"},
            [ANTHROPIC],
            "deployed mode refuses CLAUDE_CODE_OAUTH_TOKEN: a personal subscription token may"
            " not serve a deployment",
        ),
        ({}, [ANTHROPIC], "reads the API key of provider 'anthropic' from ANTHROPIC_API_KEY"),
        ({"ANTHROPIC_API_KEY": "k"}, [OPENAI], "provider 'openai' is of kind 'openai'"),
        ({"ANTHROPIC_API_KEY": "k", "CLAUDE_CODE_USE_VERTEX": "1"}, [ANTHROPIC], "not supported"),
        ({"ANTHROPIC_API_KEY": "k", "ANTHROPIC_AUTH_TOKEN": "t"}, [ANTHROPIC], "not supported"),
    ],
)
def test_deployed_mode_refuses_a_personal_token_and_what_it_cannot_run(
    environ: dict[str, str], providers: list[ModelProviderConfig], problem: str
) -> None:
    problems = credential_problems(AuthMode.DEPLOYED, environ, "http://backend:8000", providers)
    assert any(problem in line for line in problems), problems


def test_what_the_subprocess_is_handed() -> None:
    assert subprocess_environment(AuthMode.PERSONAL, ANTHROPIC, Keys()) == {}
    assert subprocess_environment(AuthMode.DEPLOYED, ANTHROPIC, Keys()) == {
        "ANTHROPIC_API_KEY": "key-of-anthropic"
    }
    assert subprocess_environment(AuthMode.DEPLOYED, GATEWAY, Keys()) == {
        "ANTHROPIC_API_KEY": "key-of-gateway",
        "ANTHROPIC_BASE_URL": "https://gateway.example/api",
    }


def test_a_refusal_names_the_variable_and_never_the_value() -> None:
    personal = refused_text(AuthMode.PERSONAL, ANTHROPIC)
    assert "CLAUDE_CODE_OAUTH_TOKEN" in personal
    assert "claude setup-token" in personal
    deployed = refused_text(AuthMode.DEPLOYED, GATEWAY)
    assert "GATEWAY_KEY" in deployed
    assert "key-of" not in deployed
