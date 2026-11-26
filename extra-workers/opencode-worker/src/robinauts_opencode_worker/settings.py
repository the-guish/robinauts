# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The worker's own settings, read from its environment: how OpenCode is run. Every problem is
reported at once, as the server reports its own.

The server's variables are not here: ``ROBINAUTS_CONFIG``, ``ROBINAUTS_WORKER_NAME`` and
``ROBINAUTS_WORKER_BROKER_URL`` are the worker module's to read, as for every worker the server
starts (``robinauts.cli.worker_process``). One of the server's is refused: the database's URL.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from robinauts.cli.composition import DATABASE_URL_VARIABLE

PREFIX = "ROBINAUTS_OPENCODE_"
WORKSPACES_VARIABLE = PREFIX + "WORKSPACES"
STATE_VARIABLE = PREFIX + "STATE"
CLI_PATH_VARIABLE = PREFIX + "CLI_PATH"
MAX_STEPS_VARIABLE = PREFIX + "MAX_STEPS"
START_SECONDS_VARIABLE = PREFIX + "START_SECONDS"
AUTH_CONTENT_VARIABLE = "OPENCODE_AUTH_CONTENT"
"""OpenCode's own: logins in place of its ``auth.json``. Not supported by this worker."""

DEFAULT_WORKSPACES = "/workspace"
DEFAULT_STATE = "~/.local/share/robinauts-opencode"
DEFAULT_CLI = "opencode"
DEFAULT_START_SECONDS = 30.0


class SettingsError(Exception):
    """The environment is not this worker's: every problem, one per line."""


@dataclass(frozen=True, slots=True)
class Settings:
    workspaces: Path
    """Where each conversation gets a directory of its own, OpenCode's project directory."""
    state: Path
    """The engine's own files: which OpenCode sessions each conversation made."""
    cli_path: str
    max_steps: int | None
    """The most model calls one Robinauts turn may take; OpenCode's own limit when unset."""
    start_seconds: float
    """How long ``opencode serve`` has to say it listens."""


def settings_from(environ: Mapping[str, str]) -> Settings:
    """The settings the environment describes; ``SettingsError`` naming every problem."""
    problems: list[str] = []

    def given(variable: str, default: str = "") -> str:
        return environ.get(variable, default).strip()

    if given(DATABASE_URL_VARIABLE):
        problems.append(
            f"{DATABASE_URL_VARIABLE} is set: this worker opens no database and reaches the"
            " server over HTTP alone; keep the database's URL out of its command"
        )
    if given(AUTH_CONTENT_VARIABLE):
        problems.append(
            f"{AUTH_CONTENT_VARIABLE} is set: this worker runs on the API keys the model"
            " providers' api_key_env name, and on no login of OpenCode's (CREDENTIALS.md)"
        )

    max_steps = None
    if text := given(MAX_STEPS_VARIABLE):
        if text.isdigit() and int(text) > 0:
            max_steps = int(text)
        else:
            problems.append(f"{MAX_STEPS_VARIABLE} must be a positive whole number")

    start_seconds = DEFAULT_START_SECONDS
    if text := given(START_SECONDS_VARIABLE):
        try:
            start_seconds = float(text)
        except ValueError:
            start_seconds = -1.0
        if not start_seconds > 0:
            problems.append(f"{START_SECONDS_VARIABLE} must be a positive number of seconds")

    if problems:
        raise SettingsError("\n".join(problems))
    return Settings(
        workspaces=Path(given(WORKSPACES_VARIABLE, DEFAULT_WORKSPACES)),
        state=Path(given(STATE_VARIABLE, DEFAULT_STATE)).expanduser(),
        cli_path=given(CLI_PATH_VARIABLE, DEFAULT_CLI),
        max_steps=max_steps,
        start_seconds=start_seconds,
    )
