# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The worker's own settings, read from its environment: which credential mode it runs in and
how Claude Code is run. Every problem is reported at once, as the server reports its own.

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

PREFIX = "ROBINAUTS_CLAUDE_"
AUTH_VARIABLE = PREFIX + "AUTH"
WORKSPACES_VARIABLE = PREFIX + "WORKSPACES"
PERMISSION_MODE_VARIABLE = PREFIX + "PERMISSION_MODE"
ALLOWED_TOOLS_VARIABLE = PREFIX + "ALLOWED_TOOLS"
DISALLOWED_TOOLS_VARIABLE = PREFIX + "DISALLOWED_TOOLS"
SETTING_SOURCES_VARIABLE = PREFIX + "SETTING_SOURCES"
SYSTEM_PROMPT_VARIABLE = PREFIX + "SYSTEM_PROMPT"
MAX_TURNS_VARIABLE = PREFIX + "MAX_TURNS"
MAX_BUDGET_VARIABLE = PREFIX + "MAX_BUDGET_USD"
EFFORT_VARIABLE = PREFIX + "EFFORT"
CLI_PATH_VARIABLE = PREFIX + "CLI_PATH"
PARTIAL_MESSAGES_VARIABLE = PREFIX + "PARTIAL_MESSAGES"

DEFAULT_WORKSPACES = "/workspace"
DEFAULT_PERMISSION_MODE = "dontAsk"
DEFAULT_ALLOWED_TOOLS = ("Read", "Edit", "Write", "Glob", "Grep", "Bash", "WebFetch", "WebSearch")
"""What runs without asking, by default: Claude Code's own tools. Nobody can answer a question
from the chat, so ``dontAsk`` denies the rest with a message the model sees."""
DEFAULT_DISALLOWED_TOOLS = ("AskUserQuestion", "ExitPlanMode")
"""The tools that wait for a person: there is none at the other end."""

PERMISSION_MODES = ("default", "acceptEdits", "plan", "bypassPermissions", "dontAsk", "auto")
SETTING_SOURCES = ("user", "project", "local")
EFFORTS = ("low", "medium", "high", "xhigh", "max")


class SettingsError(Exception):
    """The environment is not this worker's: every problem, one per line."""


class AuthMode(StrEnum):
    """How Claude Code authenticates to Anthropic: never through Robinauts (CREDENTIALS.md)."""

    PERSONAL = "personal"
    """One person, their own machine, their own subscription: a token they made themselves
    with ``claude setup-token``, in ``CLAUDE_CODE_OAUTH_TOKEN``."""
    DEPLOYED = "deployed"
    """An instance with sign-in: the operator's Claude Platform API key, read from the
    variable the model provider's ``api_key_env`` names."""


class SystemPromptMode(StrEnum):
    PRESET = "preset"
    """Claude Code's own system prompt, with the agent's appended."""
    CUSTOM = "custom"
    """The agent's system prompt alone."""


@dataclass(frozen=True, slots=True)
class Settings:
    auth: AuthMode
    workspaces: Path
    """Where each conversation gets a directory of its own, Claude Code's ``cwd``."""
    permission_mode: str
    allowed_tools: tuple[str, ...]
    disallowed_tools: tuple[str, ...]
    setting_sources: tuple[str, ...]
    system_prompt: SystemPromptMode
    max_turns: int | None
    max_budget_usd: float | None
    effort: str | None
    cli_path: Path | None
    partial_messages: bool
    """Whether the chat gets the answer's text as it is written, or per message of Claude's."""


def _names(value: str) -> tuple[str, ...]:
    return tuple(name.strip() for name in value.split(",") if name.strip())


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

    permission_mode = given(PERMISSION_MODE_VARIABLE, DEFAULT_PERMISSION_MODE)
    if permission_mode not in PERMISSION_MODES:
        problems.append(
            f"{PERMISSION_MODE_VARIABLE} {permission_mode!r} is not one of {PERMISSION_MODES}"
        )

    allowed = _names(given(ALLOWED_TOOLS_VARIABLE, ",".join(DEFAULT_ALLOWED_TOOLS)))
    disallowed = _names(given(DISALLOWED_TOOLS_VARIABLE, ",".join(DEFAULT_DISALLOWED_TOOLS)))

    sources = _names(given(SETTING_SOURCES_VARIABLE))
    if unknown := sorted(set(sources) - set(SETTING_SOURCES)):
        problems.append(
            f"{SETTING_SOURCES_VARIABLE} names {', '.join(unknown)}: only {SETTING_SOURCES}"
        )

    prompt_text = given(SYSTEM_PROMPT_VARIABLE, SystemPromptMode.PRESET.value)
    system_prompt = SystemPromptMode.PRESET
    try:
        system_prompt = SystemPromptMode(prompt_text)
    except ValueError:
        problems.append(f"{SYSTEM_PROMPT_VARIABLE} must be 'preset' or 'custom'")

    max_turns = None
    if text := given(MAX_TURNS_VARIABLE):
        if text.isdigit() and int(text) > 0:
            max_turns = int(text)
        else:
            problems.append(f"{MAX_TURNS_VARIABLE} must be a positive whole number")

    max_budget = None
    if text := given(MAX_BUDGET_VARIABLE):
        try:
            max_budget = float(text)
        except ValueError:
            max_budget = -1.0
        if not max_budget > 0:
            problems.append(f"{MAX_BUDGET_VARIABLE} must be a positive number of dollars")
            max_budget = None

    effort = given(EFFORT_VARIABLE) or None
    if effort is not None and effort not in EFFORTS:
        problems.append(f"{EFFORT_VARIABLE} {effort!r} is not one of {EFFORTS}")

    partial_text = given(PARTIAL_MESSAGES_VARIABLE, "yes").lower()
    if partial_text not in ("yes", "no"):
        problems.append(f"{PARTIAL_MESSAGES_VARIABLE} must be 'yes' or 'no'")

    if problems:
        raise SettingsError("\n".join(problems))
    cli_path = given(CLI_PATH_VARIABLE)
    return Settings(
        auth=auth,
        workspaces=Path(given(WORKSPACES_VARIABLE, DEFAULT_WORKSPACES)),
        permission_mode=permission_mode,
        allowed_tools=allowed,
        disallowed_tools=disallowed,
        setting_sources=sources,
        system_prompt=system_prompt,
        max_turns=max_turns,
        max_budget_usd=max_budget,
        effort=effort,
        cli_path=Path(cli_path) if cli_path else None,
        partial_messages=partial_text == "yes",
    )
