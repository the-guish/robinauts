# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The flows folder's settings read; and the command and the environment of neorc's worker,
which holds none of the server's secrets and the two tokens it is given."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from robinauts.controller.contract.domain import ConfigError
from robinauts.orchestrator.adapters.neorc.composed import (
    API_TOKEN_VARIABLE,
    API_URL_VARIABLE,
    NEORC_TOKEN_VARIABLE,
    check_ready,
    hidden_variables,
    worker_command,
    worker_environment,
)
from robinauts.orchestrator.contract.domain import OrchestrationConfig
from robinauts.orchestrator.core.config import parse_flows_settings
from robinauts.shared_domain.domain import (
    Config,
    ProviderConfig,
    ProviderKind,
    ToolServerConfig,
)

FOLDER = Path("/etc/robinauts/flows")
CODE = Path("/opt/robinauts/orchestration")
NEORC = ("/opt/venv/bin/neorc",)


def test_a_folder_without_settings_serves_its_flows_with_the_shipped_handlers() -> None:
    assert parse_flows_settings({}, folder=FOLDER, default_code=CODE) == OrchestrationConfig(
        code_location=CODE, flows=FOLDER
    )


def test_every_setting_is_read_and_a_relative_path_starts_at_the_folder() -> None:
    settings = {"code_location": "handlers"}
    assert parse_flows_settings(settings, folder=FOLDER, default_code=CODE) == OrchestrationConfig(
        code_location=FOLDER / "handlers", flows=FOLDER
    )
    absolute = {"code_location": "/srv/handlers"}
    assert parse_flows_settings(absolute, folder=FOLDER, default_code=CODE).code_location == Path(
        "/srv/handlers"
    )


@pytest.mark.parametrize(
    ("settings", "problem"),
    [
        ({"queue": "x"}, "flows.toml queue: unknown key"),
        ({"flows": "elsewhere"}, "flows.toml flows: unknown key"),
        ({"ui": False}, "flows.toml ui: unknown key"),
        ({"code_location": 3}, "flows.toml code_location: a path"),
        ({"code_location": ""}, "flows.toml code_location: a path"),
    ],
)
def test_wrong_settings_are_refused_with_their_problem(
    settings: dict[str, Any], problem: str
) -> None:
    with pytest.raises(ConfigError, match=problem):
        parse_flows_settings(settings, folder=FOLDER, default_code=CODE)


def test_every_problem_is_named_at_once() -> None:
    with pytest.raises(ConfigError) as refused:
        parse_flows_settings({"port": 1, "code_location": 1}, folder=FOLDER, default_code=CODE)
    assert str(refused.value).count("\n") == 1


def test_the_worker_runs_against_the_manager_on_this_server_s_worker_listener() -> None:
    described = OrchestrationConfig(code_location=CODE, flows=FOLDER)
    assert worker_command(described, command=NEORC, manager_url="http://127.0.0.1:8001") == (
        *(*NEORC, "worker", "start", "--manager-address", "http://127.0.0.1:8001"),
        *("--code-location", str(CODE), "--queue", "default"),
    )


CONFIG = Config(
    providers={"echo": ProviderConfig("echo", ProviderKind.ANTHROPIC, "ECHO_API_KEY")},
    tool_servers={
        "github": ToolServerConfig("github", "https://example.com/mcp", secret_env="GH_TOKEN"),
        "open": ToolServerConfig("open", "https://example.com/open"),
    },
)
ENVIRON = {
    "PATH": "/usr/bin",
    "ECHO_API_KEY": "sk-secret",
    "GH_TOKEN": "gh-secret",
    "ROBINAUTS_DATABASE_URL": "postgresql://u:p@h/robinauts",
    "ROBINAUTS_CONFIG": "/etc/robinauts/robinauts.toml",
    "ONEBERTH_CONFIG": "/etc/robinauts/oneberth.toml",
    NEORC_TOKEN_VARIABLE: "ob_svc_left-over-in-the-shell",
    API_TOKEN_VARIABLE: "ob_svc_left-over-in-the-shell",
    API_URL_VARIABLE: "http://elsewhere.example",
}


def test_the_worker_is_handed_no_secret_of_the_server_and_this_server_s_url() -> None:
    """The shell's own tokens, if any, give way to the two this server minted."""
    hidden = hidden_variables(CONFIG)
    assert worker_environment(
        ENVIRON,
        hidden=hidden,
        api_url="http://127.0.0.1:8000/robinauts",
        api_token="ob_svc_handlers",
        neorc_token="ob_svc_worker",
    ) == {
        "PATH": "/usr/bin",
        API_URL_VARIABLE: "http://127.0.0.1:8000/robinauts",
        API_TOKEN_VARIABLE: "ob_svc_handlers",
        NEORC_TOKEN_VARIABLE: "ob_svc_worker",
    }


def test_a_machine_without_the_flows_or_neorc_s_command_is_refused(
    tmp_path: Path,
) -> None:
    described = OrchestrationConfig(code_location=tmp_path / "code", flows=tmp_path / "flows")
    problems = check_ready(described, command=(str(tmp_path / "neorc"),))
    assert len(problems) == 3
    assert "ROBINAUTS_FLOWS_DIR: no directory" in problems[0]
    assert "flows.toml code_location: no directory" in problems[1]
    assert "no neorc command at" in problems[2]
    (tmp_path / "flows").mkdir()
    (tmp_path / "code").mkdir()
    (tmp_path / "neorc").touch()
    assert check_ready(described, command=(str(tmp_path / "neorc"),)) == []
