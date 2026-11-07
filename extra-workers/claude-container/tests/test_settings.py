# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

from __future__ import annotations

import re
from pathlib import Path

import pytest

from robinauts_claude_worker.settings import (
    AuthMode,
    SettingsError,
    SystemPromptMode,
    settings_from,
)

LEAST = {
    "ROBINAUTS_CONFIG": "/etc/robinauts/robinauts.toml",
    "ROBINAUTS_BROKER_URL": "http://127.0.0.1:8000/",
    "ROBINAUTS_WORKER_SECRET": "s3cret",
    "ROBINAUTS_CLAUDE_AUTH": "personal",
}


def test_the_least_environment_and_the_defaults() -> None:
    settings = settings_from(LEAST)
    assert settings.config == Path("/etc/robinauts/robinauts.toml")
    assert settings.broker_url == "http://127.0.0.1:8000"
    assert settings.worker == "claude-container"
    assert settings.secret == "s3cret"
    assert settings.auth is AuthMode.PERSONAL
    assert settings.workspaces == Path("/workspace")
    assert settings.permission_mode == "dontAsk"
    assert "Bash" in settings.allowed_tools
    assert settings.disallowed_tools == ("AskUserQuestion", "ExitPlanMode")
    assert settings.setting_sources == ()
    assert settings.system_prompt is SystemPromptMode.PRESET
    assert (settings.max_turns, settings.max_budget_usd, settings.effort) == (None, None, None)
    assert settings.activity_file == Path("/tmp/robinauts-claude-worker.alive")
    assert settings.cli_path is None
    assert settings.partial_messages is True


def test_every_knob(tmp_path: Path) -> None:
    secret_file = tmp_path / "secret"
    secret_file.write_text("from-a-file\n")
    settings = settings_from(
        {
            **{k: v for k, v in LEAST.items() if k != "ROBINAUTS_WORKER_SECRET"},
            "ROBINAUTS_WORKER_SECRET_FILE": str(secret_file),
            "ROBINAUTS_WORKER_NAME": "claude-2",
            "ROBINAUTS_CLAUDE_AUTH": "deployed",
            "ROBINAUTS_CLAUDE_WORKSPACES": "/srv/work",
            "ROBINAUTS_CLAUDE_PERMISSION_MODE": "bypassPermissions",
            "ROBINAUTS_CLAUDE_ALLOWED_TOOLS": "Read, Grep",
            "ROBINAUTS_CLAUDE_DISALLOWED_TOOLS": "",
            "ROBINAUTS_CLAUDE_SETTING_SOURCES": "project,local",
            "ROBINAUTS_CLAUDE_SYSTEM_PROMPT": "custom",
            "ROBINAUTS_CLAUDE_MAX_TURNS": "40",
            "ROBINAUTS_CLAUDE_MAX_BUDGET_USD": "2.5",
            "ROBINAUTS_CLAUDE_EFFORT": "high",
            "ROBINAUTS_CLAUDE_ACTIVITY_FILE": "/run/alive",
            "ROBINAUTS_CLAUDE_CLI_PATH": "/usr/local/bin/claude",
            "ROBINAUTS_CLAUDE_PARTIAL_MESSAGES": "no",
        }
    )
    assert settings.secret == "from-a-file"
    assert settings.worker == "claude-2"
    assert settings.auth is AuthMode.DEPLOYED
    assert settings.workspaces == Path("/srv/work")
    assert settings.permission_mode == "bypassPermissions"
    assert settings.allowed_tools == ("Read", "Grep")
    assert settings.disallowed_tools == ()
    assert settings.setting_sources == ("project", "local")
    assert settings.system_prompt is SystemPromptMode.CUSTOM
    assert (settings.max_turns, settings.max_budget_usd, settings.effort) == (40, 2.5, "high")
    assert settings.activity_file == Path("/run/alive")
    assert settings.cli_path == Path("/usr/local/bin/claude")
    assert settings.partial_messages is False


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        ({"ROBINAUTS_CONFIG": ""}, "ROBINAUTS_CONFIG is not set"),
        ({"ROBINAUTS_BROKER_URL": "backend:8000"}, "ROBINAUTS_BROKER_URL is not an http or https"),
        ({"ROBINAUTS_BROKER_URL": ""}, "ROBINAUTS_BROKER_URL is not an http or https"),
        (
            {"ROBINAUTS_WORKER_NAME": "Claude"},
            "ROBINAUTS_WORKER_NAME 'Claude' is not a worker name",
        ),
        ({"ROBINAUTS_WORKER_SECRET": ""}, "ROBINAUTS_WORKER_SECRET is not set"),
        (
            {"ROBINAUTS_DATABASE_URL": "postgresql://robinauts@db/robinauts"},
            "ROBINAUTS_DATABASE_URL is set: this worker opens no database",
        ),
        (
            {"ROBINAUTS_WORKER_SECRET_FILE": "/nowhere/secret"},
            "ROBINAUTS_WORKER_SECRET and ROBINAUTS_WORKER_SECRET_FILE are both set",
        ),
        ({"ROBINAUTS_CLAUDE_AUTH": ""}, "ROBINAUTS_CLAUDE_AUTH must be 'personal' or 'deployed'"),
        (
            {"ROBINAUTS_CLAUDE_AUTH": "both"},
            "ROBINAUTS_CLAUDE_AUTH must be 'personal' or 'deployed'",
        ),
        ({"ROBINAUTS_CLAUDE_PERMISSION_MODE": "yolo"}, "ROBINAUTS_CLAUDE_PERMISSION_MODE 'yolo'"),
        ({"ROBINAUTS_CLAUDE_SETTING_SOURCES": "user,global"}, "names global: only"),
        ({"ROBINAUTS_CLAUDE_SYSTEM_PROMPT": "none"}, "must be 'preset' or 'custom'"),
        ({"ROBINAUTS_CLAUDE_MAX_TURNS": "0"}, "ROBINAUTS_CLAUDE_MAX_TURNS must be a positive"),
        ({"ROBINAUTS_CLAUDE_MAX_TURNS": "ten"}, "ROBINAUTS_CLAUDE_MAX_TURNS must be a positive"),
        ({"ROBINAUTS_CLAUDE_MAX_BUDGET_USD": "-1"}, "must be a positive number of dollars"),
        ({"ROBINAUTS_CLAUDE_MAX_BUDGET_USD": "lots"}, "must be a positive number of dollars"),
        ({"ROBINAUTS_CLAUDE_EFFORT": "ultra"}, "ROBINAUTS_CLAUDE_EFFORT 'ultra' is not one of"),
        ({"ROBINAUTS_CLAUDE_PARTIAL_MESSAGES": "maybe"}, "must be 'yes' or 'no'"),
    ],
)
def test_a_wrong_variable_is_refused(change: dict[str, str], problem: str) -> None:
    with pytest.raises(SettingsError, match=re.escape(problem)):
        settings_from({**LEAST, **change})


def test_every_problem_is_named_at_once() -> None:
    with pytest.raises(SettingsError) as raised:
        settings_from({})
    lines = str(raised.value).splitlines()
    assert len(lines) == 4
    assert lines[0].startswith("ROBINAUTS_CONFIG")
    assert lines[-1].startswith("ROBINAUTS_CLAUDE_AUTH")


def test_a_missing_or_empty_secret_file_is_refused(tmp_path: Path) -> None:
    least = {k: v for k, v in LEAST.items() if k != "ROBINAUTS_WORKER_SECRET"}
    with pytest.raises(SettingsError, match="ROBINAUTS_WORKER_SECRET_FILE: "):
        settings_from({**least, "ROBINAUTS_WORKER_SECRET_FILE": str(tmp_path / "none")})
    empty = tmp_path / "empty"
    empty.write_text("\n")
    with pytest.raises(SettingsError, match="names an empty file"):
        settings_from({**least, "ROBINAUTS_WORKER_SECRET_FILE": str(empty)})
