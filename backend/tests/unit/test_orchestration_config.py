# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The flows folder's settings read; the command and the environment of neorc's worker, which
holds none of the server's secrets; and the policy that lets this server frame neorc's UI."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from robinauts.controller.contract.domain import ConfigError
from robinauts.orchestrator.adapters.neorc.embedded import (
    API_TOKEN_VARIABLE,
    API_URL_VARIABLE,
    _framable_policy,
    check_ready,
    hidden_variables,
    worker_command,
    worker_environment,
)
from robinauts.orchestrator.contract.domain import OrchestrationConfig
from robinauts.orchestrator.core.config import parse_flows_settings
from robinauts.shared_domain.domain import Config, ProviderConfig, ProviderKind, ToolServerConfig

FOLDER = Path("/etc/robinauts/flows")
CODE = Path("/opt/robinauts/orchestration")
NEORC = ("/opt/venv/bin/neorc",)


def test_a_folder_without_settings_serves_its_flows_with_the_shipped_handlers_and_the_ui() -> None:
    assert parse_flows_settings({}, folder=FOLDER, default_code=CODE) == OrchestrationConfig(
        code_location=CODE, flows=FOLDER, ui=True
    )


def test_every_setting_is_read_and_a_relative_path_starts_at_the_folder() -> None:
    settings = {"code_location": "handlers", "ui": False}
    assert parse_flows_settings(settings, folder=FOLDER, default_code=CODE) == OrchestrationConfig(
        code_location=FOLDER / "handlers", flows=FOLDER, ui=False
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
        ({"ui": "no"}, "flows.toml ui: true or false"),
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
        parse_flows_settings({"port": 1, "ui": 1}, folder=FOLDER, default_code=CODE)
    assert str(refused.value).count("\n") == 1


def test_the_worker_runs_against_the_manager_mounted_on_this_server() -> None:
    described = OrchestrationConfig(code_location=CODE, flows=FOLDER)
    assert worker_command(described, command=NEORC, api_url="http://127.0.0.1:8000") == (
        *(*NEORC, "worker", "start", "--manager-address", "http://127.0.0.1:8000/neorc"),
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
    API_TOKEN_VARIABLE: "rbt_left-over-in-the-shell",
    API_URL_VARIABLE: "http://elsewhere.example",
}


def test_the_worker_is_handed_no_secret_of_the_server_and_this_server_s_url() -> None:
    """The shell's own API token, if any, is not handed on either: this server takes none."""
    hidden = hidden_variables(CONFIG)
    assert worker_environment(ENVIRON, hidden=hidden, api_url="http://127.0.0.1:8000") == {
        "PATH": "/usr/bin",
        API_URL_VARIABLE: "http://127.0.0.1:8000",
    }


def test_a_machine_without_the_flows_neorc_s_command_or_its_ui_is_refused(
    tmp_path: Path,
) -> None:
    described = OrchestrationConfig(code_location=tmp_path / "code", flows=tmp_path / "flows")
    problems = check_ready(described, command=(str(tmp_path / "neorc"),), ui=None)
    assert len(problems) == 4
    assert "ROBINAUTS_FLOWS_DIR: no directory" in problems[0]
    assert "flows.toml code_location: no directory" in problems[1]
    assert "no neorc command at" in problems[2]
    assert "flows.toml ui: the installed neorc-ui holds no built UI" in problems[3]
    (tmp_path / "flows").mkdir()
    (tmp_path / "code").mkdir()
    (tmp_path / "neorc").touch()
    assert check_ready(described, command=(str(tmp_path / "neorc"),), ui=tmp_path) == []
    without_ui = OrchestrationConfig(
        code_location=tmp_path / "code", flows=tmp_path / "flows", ui=False
    )
    assert check_ready(without_ui, command=(str(tmp_path / "neorc"),), ui=None) == []


@pytest.mark.parametrize(
    ("policy", "framable"),
    [
        (
            b"default-src 'none'; script-src 'self'; frame-ancestors 'none'",
            b"default-src 'none'; script-src 'self'; frame-ancestors 'self'",
        ),
        (
            b"frame-ancestors 'none'; connect-src 'self';",
            b"connect-src 'self'; frame-ancestors 'self'",
        ),
        (b"default-src 'none'", b"default-src 'none'; frame-ancestors 'self'"),
    ],
)
def test_neorc_s_ui_policy_is_kept_but_for_who_may_frame_it(policy: bytes, framable: bytes) -> None:
    assert _framable_policy(policy) == framable
