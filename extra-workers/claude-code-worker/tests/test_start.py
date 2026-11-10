# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""What the worker checks before it hands over to the backend's worker module, without a
server: the server's variables, its own, the configuration and the credential mode. Every
problem is a line; none lets the start go on."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from robinauts.cli.composition import configure
from robinauts_claude_worker.__main__ import (
    configuration_problems,
    environment_problems,
    problems_of,
    providers_of,
)

CONFIG = """
[model_providers.anthropic]
kind = "anthropic"
api_key_env = "ANTHROPIC_API_KEY"

[model_providers.openai]
kind = "openai"
api_key_env = "OPENAI_API_KEY"

[models.opus]
provider = "anthropic"
name = "claude-opus-5-5"

[models.gpt]
provider = "openai"
name = "gpt-x"

[agents.coder]
title = "Coder"
system_prompt = "You work in this conversation's directory."
model = "opus"
engine = "claude-code"
worker = "claude"

[agents.helper]
title = "Helper"
system_prompt = "You help."
model = "gpt"
engine = "pydantic-ai"

[workers.claude]
command = ["docker", "run", "-i", "robinauts-claude-code-worker"]
"""

SERVER = {
    "ROBINAUTS_CONFIG": "/etc/robinauts/robinauts.toml",
    "ROBINAUTS_WORKER_NAME": "claude",
    "ROBINAUTS_WORKER_BROKER_URL": "http://127.0.0.1:8000",
}
"""What the server's environment holds when it starts the worker."""


def parsed(text: str = CONFIG):
    import tomllib

    config, _ = configure(tomllib.loads(text), {})
    return config


def test_the_servers_variables_are_needed() -> None:
    assert environment_problems(SERVER) == []
    problems = environment_problems({})
    assert len(problems) == 2
    assert problems[0].startswith("ROBINAUTS_CONFIG is not set")
    assert problems[1].startswith("ROBINAUTS_WORKER_BROKER_URL is not set")


def test_the_configuration_must_give_this_worker_its_agents() -> None:
    config = parsed()
    assert configuration_problems("claude", config) == []
    assert configuration_problems("nobody", config) == [
        "no agent of the configuration names worker 'nobody'"
    ]
    moved = CONFIG.replace('engine = "pydantic-ai"', 'engine = "pydantic-ai"\nworker = "claude"')
    another_engine = parsed(moved)
    (problem,) = configuration_problems("claude", another_engine)
    assert problem.startswith("agent 'helper' runs on engine 'pydantic-ai'")


def test_the_providers_of_this_workers_agents_alone() -> None:
    assert [provider.id for provider in providers_of("claude", parsed())] == ["anthropic"]
    assert [provider.id for provider in providers_of("default", parsed())] == ["openai"]


def test_everything_is_checked_in_order(tmp_path: Path) -> None:
    config = tmp_path / "robinauts.toml"
    config.write_text(CONFIG)
    server = {**SERVER, "ROBINAUTS_CONFIG": str(config)}
    # The environment first, the server's variables, every extra worker's and this one's together.
    problems = problems_of({"ROBINAUTS_CLAUDE_MAX_TURNS": "x", "GITHUB_TOKEN": ""})
    assert [line.split(" ")[0] for line in problems] == [
        "ROBINAUTS_CONFIG",
        "ROBINAUTS_WORKER_BROKER_URL",
        "GITHUB_TOKEN",
        "ROBINAUTS_CLAUDE_AUTH",
        "ROBINAUTS_CLAUDE_MAX_TURNS",
    ]
    # Then the configuration file.
    personal = {**server, "ROBINAUTS_CLAUDE_AUTH": "personal"}
    (problem,) = problems_of({**personal, "ROBINAUTS_CONFIG": str(tmp_path / "none.toml")})
    assert problem.startswith("ROBINAUTS_CONFIG: ")
    (problem,) = problems_of({**personal, "ROBINAUTS_WORKER_NAME": "nobody"})
    assert problem == "no agent of the configuration names worker 'nobody'"
    # Then the credential mode, against the providers of this worker's agents.
    (problem,) = problems_of(personal)
    assert problem.startswith("personal mode needs CLAUDE_CODE_OAUTH_TOKEN")
    assert problems_of({**personal, "CLAUDE_CODE_OAUTH_TOKEN": "t"}) == []
    github = {**personal, "CLAUDE_CODE_OAUTH_TOKEN": "t", "GITHUB_TOKEN": "github_pat_x"}
    assert problems_of(github) == []
    (problem,) = problems_of({**server, "ROBINAUTS_CLAUDE_AUTH": "deployed"})
    assert problem.startswith("deployed mode reads the API key of provider 'anthropic'")
    deployed = {**server, "ROBINAUTS_CLAUDE_AUTH": "deployed", "ANTHROPIC_API_KEY": "k"}
    assert problems_of(deployed) == []


@pytest.mark.parametrize(
    ("environment", "said"),
    [
        ({}, "ROBINAUTS_CONFIG is not set"),
        ({**SERVER, "ROBINAUTS_CLAUDE_AUTH": "deployed"}, "deployed mode reads the API key"),
    ],
)
def test_the_process_refuses_with_exit_code_2_before_reading_its_secret(
    tmp_path: Path, environment: dict[str, str], said: str
) -> None:
    config = tmp_path / "robinauts.toml"
    config.write_text(CONFIG)
    if "ROBINAUTS_CONFIG" in environment:
        environment = {**environment, "ROBINAUTS_CONFIG": str(config)}
    ran = subprocess.run(
        [sys.executable, "-m", "robinauts_claude_worker"],
        env={"PATH": "/usr/bin:/bin", **environment},
        input=b"the secret",
        capture_output=True,
        timeout=120,
    )
    assert ran.returncode == 2, ran.stderr.decode()
    assert said in ran.stderr.decode()
    assert ran.stdout == b""
