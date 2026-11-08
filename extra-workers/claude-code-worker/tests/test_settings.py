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

LEAST = {"ROBINAUTS_CLAUDE_AUTH": "personal"}


def test_the_least_environment_and_the_defaults() -> None:
    settings = settings_from(LEAST)
    assert settings.auth is AuthMode.PERSONAL
    assert settings.workspaces == Path("/workspace")
    assert settings.permission_mode == "dontAsk"
    assert "Bash" in settings.allowed_tools
    assert settings.disallowed_tools == ("AskUserQuestion", "ExitPlanMode")
    assert settings.setting_sources == ()
    assert settings.system_prompt is SystemPromptMode.PRESET
    assert (settings.max_turns, settings.max_budget_usd, settings.effort) == (None, None, None)
    assert settings.cli_path is None
    assert settings.partial_messages is True


def test_the_servers_variables_are_not_its_concern() -> None:
    """The worker module reads them; this worker refuses the one that is not for it."""
    settings = settings_from(
        {
            **LEAST,
            "ROBINAUTS_CONFIG": "/etc/robinauts/robinauts.toml",
            "ROBINAUTS_WORKER_NAME": "claude",
            "ROBINAUTS_WORKER_BROKER_URL": "http://127.0.0.1:8000",
            "ROBINAUTS_WORKER_PARENT": "1",
        }
    )
    assert settings.auth is AuthMode.PERSONAL


def test_every_knob() -> None:
    settings = settings_from(
        {
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
            "ROBINAUTS_CLAUDE_CLI_PATH": "/usr/local/bin/claude",
            "ROBINAUTS_CLAUDE_PARTIAL_MESSAGES": "no",
        }
    )
    assert settings.auth is AuthMode.DEPLOYED
    assert settings.workspaces == Path("/srv/work")
    assert settings.permission_mode == "bypassPermissions"
    assert settings.allowed_tools == ("Read", "Grep")
    assert settings.disallowed_tools == ()
    assert settings.setting_sources == ("project", "local")
    assert settings.system_prompt is SystemPromptMode.CUSTOM
    assert (settings.max_turns, settings.max_budget_usd, settings.effort) == (40, 2.5, "high")
    assert settings.cli_path == Path("/usr/local/bin/claude")
    assert settings.partial_messages is False


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        (
            {"ROBINAUTS_DATABASE_URL": "postgresql://robinauts@db/robinauts"},
            "ROBINAUTS_DATABASE_URL is set: this worker opens no database",
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
        settings_from(
            {
                "ROBINAUTS_DATABASE_URL": "postgresql://robinauts@db/robinauts",
                "ROBINAUTS_CLAUDE_MAX_TURNS": "-1",
                "ROBINAUTS_CLAUDE_EFFORT": "ultra",
            }
        )
    lines = str(raised.value).splitlines()
    assert len(lines) == 4
    assert lines[0].startswith("ROBINAUTS_DATABASE_URL")
    assert lines[1].startswith("ROBINAUTS_CLAUDE_AUTH")
    assert lines[-1].startswith("ROBINAUTS_CLAUDE_EFFORT")
