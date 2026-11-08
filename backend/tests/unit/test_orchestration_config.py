# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The ``[orchestration]`` table read, and what neorc's processes are started with: the
commands, in order, and an environment that holds none of the server's secrets."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from robinauts.cli.orchestration import (
    API_TOKEN_VARIABLE,
    API_URL_VARIABLE,
    OrchestrationConfig,
    check_ready,
    hidden_variables,
    neorc_commands,
    neorc_environment,
    parse_orchestration,
)
from robinauts.controller.contract.domain import ConfigError
from robinauts.shared_domain.domain import Config, ProviderConfig, ProviderKind, ToolServerConfig

BASE = Path("/etc/robinauts")
CODE = Path("/opt/robinauts/orchestration")


def test_an_empty_table_runs_neorc_from_the_path_with_the_shipped_flows() -> None:
    assert parse_orchestration({}, base=BASE, default_code=CODE) == OrchestrationConfig(
        code_location=CODE, flows=CODE / "flows"
    )


def test_every_key_is_read_and_relative_paths_start_at_the_configuration_file() -> None:
    table = {
        "command": ["uv", "run", "neorc"],
        "port": 9000,
        "database_url_env": "MY_NEORC_DB",
        "code_location": "work",
        "flows": "/srv/flows",
        "ui": False,
    }
    assert parse_orchestration(table, base=BASE, default_code=CODE) == OrchestrationConfig(
        code_location=BASE / "work",
        flows=Path("/srv/flows"),
        command=("uv", "run", "neorc"),
        port=9000,
        database_url_env="MY_NEORC_DB",
        ui=False,
    )


@pytest.mark.parametrize(
    ("table", "problem"),
    [
        ({"queue": "x"}, "queue: unknown key"),
        ({"command": "neorc"}, "command: a non-empty list"),
        ({"command": []}, "command: a non-empty list"),
        ({"command": ["neorc", ""]}, "command: a non-empty list"),
        ({"port": 0}, "port: a port number"),
        ({"port": True}, "port: a port number"),
        ({"port": "8420"}, "port: a port number"),
        ({"database_url_env": ""}, "database_url_env: the name"),
        ({"ui": "no"}, "ui: true or false"),
        ({"flows": 3}, "flows: a path"),
        ({"code_location": ""}, "code_location: a path"),
    ],
)
def test_a_wrong_table_is_refused_with_its_problem(table: dict[str, Any], problem: str) -> None:
    with pytest.raises(ConfigError, match=problem):
        parse_orchestration(table, base=BASE, default_code=CODE)


def test_every_problem_is_named_at_once() -> None:
    with pytest.raises(ConfigError) as refused:
        parse_orchestration({"port": -1, "ui": 1}, base=BASE, default_code=CODE)
    assert str(refused.value).count("\n") == 1


def test_the_commands_run_the_manager_on_loopback_with_no_authentication() -> None:
    described = OrchestrationConfig(
        code_location=CODE, flows=CODE / "flows", command=("uv", "run", "neorc"), ui=False
    )
    address = ("--manager-address", "http://127.0.0.1:8420")
    assert neorc_commands(described) == {
        "manager": (
            *("uv", "run", "neorc", "manager", "start", "--host", "127.0.0.1", "--port", "8420"),
            *("--no-auth", "--create-schema", "--no-ui"),
        ),
        "upload": ("uv", "run", "neorc", "flows", "upload", str(CODE / "flows"), *address),
        "scheduler": ("uv", "run", "neorc", "scheduler", "start", *address),
        "worker": (
            *("uv", "run", "neorc", "worker", "start", *address),
            *("--code-location", str(CODE), "--queue", "default"),
        ),
    }


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
    "MY_NEORC_DB": "postgresql://u:p@h/neorc",
    API_TOKEN_VARIABLE: "rbt_left-over-in-the-shell",
}
DESCRIBED = OrchestrationConfig(
    code_location=CODE, flows=CODE / "flows", database_url_env="MY_NEORC_DB"
)


@pytest.mark.parametrize(
    ("api_url", "expected"),
    [
        (
            None,
            {
                "PATH": "/usr/bin",
                "MY_NEORC_DB": "postgresql://u:p@h/neorc",
                "NEORC_DATABASE_URL": "postgresql://u:p@h/neorc",
            },
        ),
        (
            "http://127.0.0.1:8000",
            {
                "PATH": "/usr/bin",
                "MY_NEORC_DB": "postgresql://u:p@h/neorc",
                "NEORC_DATABASE_URL": "postgresql://u:p@h/neorc",
                API_URL_VARIABLE: "http://127.0.0.1:8000",
            },
        ),
    ],
)
def test_neorc_is_handed_no_secret_of_the_server_and_the_worker_alone_the_api_url(
    api_url: str | None, expected: dict[str, str]
) -> None:
    """The shell's own API token, if any, is not handed on either: this server takes none."""
    hidden = hidden_variables(CONFIG)
    assert neorc_environment(ENVIRON, DESCRIBED, hidden=hidden, api_url=api_url) == expected


def test_a_machine_without_neorc_s_database_or_its_flows_is_refused(tmp_path: Path) -> None:
    described = OrchestrationConfig(code_location=tmp_path, flows=tmp_path / "flows")
    problems = check_ready(described, {})
    assert len(problems) == 2
    assert "set NEORC_DATABASE_URL" in problems[0]
    assert "flows: no directory" in problems[1]
    (tmp_path / "flows").mkdir()
    assert check_ready(described, {"NEORC_DATABASE_URL": "postgresql://h/neorc"}) == []
