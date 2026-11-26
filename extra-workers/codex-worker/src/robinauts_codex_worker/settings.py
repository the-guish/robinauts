# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The worker's own settings, read from its environment: which credential mode it runs in and
how Codex is run. Every problem is reported at once, as the server reports its own.

The server's variables are not here: ``ROBINAUTS_CONFIG``, ``ROBINAUTS_WORKER_NAME`` and
``ROBINAUTS_WORKER_BROKER_URL`` are the worker module's to read, as for every worker the server
starts (``robinauts.cli.worker_process``). One of the server's is refused: the database's URL.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from robinauts.cli.composition import DATABASE_URL_VARIABLE

PREFIX = "ROBINAUTS_CODEX_"
AUTH_VARIABLE = PREFIX + "AUTH"
WORKSPACES_VARIABLE = PREFIX + "WORKSPACES"
SANDBOX_VARIABLE = PREFIX + "SANDBOX"
SYSTEM_PROMPT_VARIABLE = PREFIX + "SYSTEM_PROMPT"
EFFORT_VARIABLE = PREFIX + "EFFORT"
SUMMARY_VARIABLE = PREFIX + "REASONING_SUMMARY"
CLI_PATH_VARIABLE = PREFIX + "CLI_PATH"
HOME_VARIABLE = "CODEX_HOME"
"""Codex's own: where it keeps its threads, its state and, in personal mode, the login."""

DEFAULT_WORKSPACES = "/workspace"
DEFAULT_HOME = "~/.codex"
DEFAULT_CLI = "codex"
DEFAULT_SANDBOX = "danger-full-access"
"""The container is the sandbox: Codex's own needs user namespaces, which a container lacks."""

SANDBOXES = ("read-only", "workspace-write", "danger-full-access")
EFFORTS = ("minimal", "low", "medium", "high", "xhigh")
SUMMARIES = ("auto", "concise", "detailed", "none")


class SettingsError(Exception):
    """The environment is not this worker's: every problem, one per line."""


class AuthMode(StrEnum):
    """How Codex authenticates to OpenAI: never through Robinauts (CREDENTIALS.md)."""

    PERSONAL = "personal"
    """One person, their own machine, their own ChatGPT plan: the login they made themselves
    with ``codex login``, in ``$CODEX_HOME/auth.json``."""
    DEPLOYED = "deployed"
    """An instance with sign-in: the operator's API key, read from the variable the model
    provider's ``api_key_env`` names."""


class SystemPromptMode(StrEnum):
    PRESET = "preset"
    """Codex's own instructions, with the agent's system prompt as developer instructions."""
    CUSTOM = "custom"
    """The agent's system prompt in place of Codex's own instructions."""


@dataclass(frozen=True, slots=True)
class Settings:
    auth: AuthMode
    workspaces: Path
    """Where each conversation gets a directory of its own, Codex's ``cwd``."""
    home: Path
    """``CODEX_HOME``: the threads, Codex's state, and the personal mode's login."""
    sandbox: str
    system_prompt: SystemPromptMode
    effort: str | None
    summary: str
    cli_path: str


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

    auth_text = given(AUTH_VARIABLE)
    auth = AuthMode.PERSONAL
    try:
        auth = AuthMode(auth_text)
    except ValueError:
        problems.append(
            f"{AUTH_VARIABLE} must be 'personal' or 'deployed' (CREDENTIALS.md); it is"
            f" {auth_text!r}"
        )

    sandbox = given(SANDBOX_VARIABLE, DEFAULT_SANDBOX)
    if sandbox not in SANDBOXES:
        problems.append(f"{SANDBOX_VARIABLE} {sandbox!r} is not one of {SANDBOXES}")

    prompt_text = given(SYSTEM_PROMPT_VARIABLE, SystemPromptMode.PRESET.value)
    system_prompt = SystemPromptMode.PRESET
    try:
        system_prompt = SystemPromptMode(prompt_text)
    except ValueError:
        problems.append(f"{SYSTEM_PROMPT_VARIABLE} must be 'preset' or 'custom'")

    effort = given(EFFORT_VARIABLE) or None
    if effort is not None and effort not in EFFORTS:
        problems.append(f"{EFFORT_VARIABLE} {effort!r} is not one of {EFFORTS}")

    summary = given(SUMMARY_VARIABLE, "auto")
    if summary not in SUMMARIES:
        problems.append(f"{SUMMARY_VARIABLE} {summary!r} is not one of {SUMMARIES}")

    if problems:
        raise SettingsError("\n".join(problems))
    return Settings(
        auth=auth,
        workspaces=Path(given(WORKSPACES_VARIABLE, DEFAULT_WORKSPACES)),
        home=Path(given(HOME_VARIABLE, DEFAULT_HOME)).expanduser(),
        sandbox=sandbox,
        system_prompt=system_prompt,
        effort=effort,
        summary=summary,
        cli_path=given(CLI_PATH_VARIABLE, DEFAULT_CLI),
    )
