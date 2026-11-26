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

from robinauts_codex_worker.__main__ import launched_state, problems_of

CONFIG = """
[model_providers.openai]
kind = "openai"
api_key_env = "OPENAI_API_KEY"

[model_providers.anthropic]
kind = "anthropic"
api_key_env = "ANTHROPIC_API_KEY"

[models.gpt]
provider = "openai"
name = "gpt-5.5"

[models.opus]
provider = "anthropic"
name = "claude-opus-5-5"

[agents.coder]
title = "Coder"
system_prompt = "You work in this conversation's directory."
models = ["gpt"]
engine = "codex"
worker = "codex"

[agents.helper]
title = "Helper"
system_prompt = "You help."
models = ["opus"]
engine = "pydantic-ai"

[workers.codex]
command = ["docker", "run", "-i", "robinauts-codex-worker"]
"""

SERVER = {
    "ROBINAUTS_CONFIG": "/etc/robinauts/robinauts.toml",
    "ROBINAUTS_WORKER_NAME": "codex",
    "ROBINAUTS_WORKER_BROKER_URL": "http://127.0.0.1:8000",
}
LAUNCHED = {
    "ROBINAUTS_CONFIG": "/data/workers/codex/robinauts.toml",
    "ROBINAUTS_DATA_DIR": "/data",
    "ONEBERTH_WORKER_NAME": "codex",
    "ONEBERTH_WORKER_URL": "http://172.18.0.2:8421",
}


@pytest.mark.parametrize(
    ("environ", "state"),
    [
        (
            LAUNCHED,
            {
                "CODEX_HOME": "/data/workers/codex/codex",
                "ROBINAUTS_CODEX_WORKSPACES": "/data/workers/codex/workspaces",
            },
        ),
        ({**LAUNCHED, "ROBINAUTS_DATA_DIR": ""}, {}),
        (SERVER, {}),
    ],
)
def test_under_the_launcher_the_state_is_in_the_workers_folder(
    environ: dict[str, str], state: dict[str, str]
) -> None:
    assert launched_state(environ) == state


def test_everything_is_checked_in_order(tmp_path: Path) -> None:
    config = tmp_path / "robinauts.toml"
    config.write_text(CONFIG)
    home = tmp_path / "codex"
    home.mkdir()
    server = {**SERVER, "ROBINAUTS_CONFIG": str(config), "CODEX_HOME": str(home)}
    problems = problems_of({"ROBINAUTS_CODEX_EFFORT": "x", "GITHUB_TOKEN": ""})
    assert [line.split(" ")[0] for line in problems] == [
        "ROBINAUTS_CONFIG",
        "ROBINAUTS_WORKER_BROKER_URL",
        "GITHUB_TOKEN",
        "ROBINAUTS_CODEX_AUTH",
        "ROBINAUTS_CODEX_EFFORT",
    ]
    personal = {**server, "ROBINAUTS_CODEX_AUTH": "personal"}
    (problem,) = problems_of({**personal, "ROBINAUTS_WORKER_NAME": "nobody"})
    assert problem == "no agent of the configuration names worker 'nobody'"
    moved = CONFIG.replace('engine = "pydantic-ai"', 'engine = "pydantic-ai"\nworker = "codex"')
    config.write_text(moved)
    (problem,) = problems_of(personal)
    assert problem.startswith("agent 'helper' runs on engine 'pydantic-ai'")
    config.write_text(CONFIG)
    (problem,) = problems_of(personal)
    assert problem.startswith("personal mode needs")
    (home / "auth.json").write_text("{}")
    assert problems_of(personal) == []
    refused, unset = problems_of({**server, "ROBINAUTS_CODEX_AUTH": "deployed"})
    assert refused.startswith("deployed mode refuses")
    assert unset.startswith("deployed mode reads the API key")
    (home / "auth.json").unlink()
    (problem,) = problems_of({**server, "ROBINAUTS_CODEX_AUTH": "deployed"})
    assert problem.startswith("deployed mode reads the API key of provider 'openai'")
    deployed = {**server, "ROBINAUTS_CODEX_AUTH": "deployed", "OPENAI_API_KEY": "k"}
    assert problems_of(deployed) == []


def test_the_process_refuses_with_exit_code_2_before_reading_its_secret(tmp_path: Path) -> None:
    config = tmp_path / "robinauts.toml"
    config.write_text(CONFIG)
    environment = {**SERVER, "ROBINAUTS_CONFIG": str(config), "ROBINAUTS_CODEX_AUTH": "deployed"}
    ran = subprocess.run(
        [sys.executable, "-m", "robinauts_codex_worker"],
        env={"PATH": "/usr/bin:/bin", "CODEX_HOME": str(tmp_path), **environment},
        input=b"the secret",
        capture_output=True,
        timeout=120,
    )
    assert ran.returncode == 2, ran.stderr.decode()
    assert "deployed mode reads the API key" in ran.stderr.decode()
    assert ran.stdout == b""
