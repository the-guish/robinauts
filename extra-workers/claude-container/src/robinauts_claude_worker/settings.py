# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The worker's own settings, read from its environment: where the server is, which worker this
is, the secret it presents, which credential mode it runs in, and how Claude Code is run. Every
problem is reported at once, as the server reports its own."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from urllib.parse import urlsplit

from robinauts.controller.core.config import WORKER_NAME

CONFIG_VARIABLE = "ROBINAUTS_CONFIG"
"""The deployment's TOML file, the same the server reads: this worker's agents are in it."""

BROKER_URL_VARIABLE = "ROBINAUTS_BROKER_URL"
"""The root of the server, as this worker reaches it."""

WORKER_NAME_VARIABLE = "ROBINAUTS_WORKER_NAME"
"""The worker this process is: a ``[workers.<name>]`` table of kind ``external``."""

SECRET_VARIABLE = "ROBINAUTS_WORKER_SECRET"
"""The secret the server's ``secret_env`` variable holds, which this worker presents."""

SECRET_FILE_VARIABLE = "ROBINAUTS_WORKER_SECRET_FILE"
"""A file that holds the secret instead, such as a Docker secret; one of the two."""

DATABASE_URL_VARIABLE = "ROBINAUTS_DATABASE_URL"
"""The server's, never this worker's: it opens no database and reaches the core over HTTP
alone, so a database URL in its environment is a mistake it refuses to start with."""

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
ACTIVITY_FILE_VARIABLE = PREFIX + "ACTIVITY_FILE"
CLI_PATH_VARIABLE = PREFIX + "CLI_PATH"
PARTIAL_MESSAGES_VARIABLE = PREFIX + "PARTIAL_MESSAGES"

DEFAULT_WORKER_NAME = "claude-container"
DEFAULT_WORKSPACES = "/workspace"
DEFAULT_ACTIVITY_FILE = "/tmp/robinauts-claude-worker.alive"
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
    """The environment is not a worker's: every problem, one per line."""


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
    config: Path
    broker_url: str
    worker: str
    secret: str
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
    activity_file: Path
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

    config = given(CONFIG_VARIABLE)
    if not config:
        problems.append(f"{CONFIG_VARIABLE} is not set: the deployment's TOML file")

    broker_url = given(BROKER_URL_VARIABLE).rstrip("/")
    parts = urlsplit(broker_url) if broker_url else None
    if parts is None or parts.scheme not in ("http", "https") or not parts.netloc:
        problems.append(
            f"{BROKER_URL_VARIABLE} is not an http or https URL: the server this worker reaches"
        )

    if given(DATABASE_URL_VARIABLE):
        problems.append(
            f"{DATABASE_URL_VARIABLE} is set: this worker opens no database and reaches the"
            " server over HTTP alone; keep the database's URL out of its environment"
        )

    worker = given(WORKER_NAME_VARIABLE, DEFAULT_WORKER_NAME)
    if not WORKER_NAME.fullmatch(worker):
        problems.append(f"{WORKER_NAME_VARIABLE} {worker!r} is not a worker name")

    secret = given(SECRET_VARIABLE)
    secret_file = given(SECRET_FILE_VARIABLE)
    if secret and secret_file:
        problems.append(f"{SECRET_VARIABLE} and {SECRET_FILE_VARIABLE} are both set: one of them")
    elif secret_file:
        try:
            secret = Path(secret_file).read_text().strip()
        except OSError as error:
            problems.append(f"{SECRET_FILE_VARIABLE}: {error}")
        if secret_file and not secret and not problems[-1:]:
            problems.append(f"{SECRET_FILE_VARIABLE} names an empty file")
    if not secret and not secret_file:
        problems.append(
            f"{SECRET_VARIABLE} is not set: the secret the server's secret_env variable holds"
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
        config=Path(config),
        broker_url=broker_url,
        worker=worker,
        secret=secret,
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
        activity_file=Path(given(ACTIVITY_FILE_VARIABLE, DEFAULT_ACTIVITY_FILE)),
        cli_path=Path(cli_path) if cli_path else None,
        partial_messages=partial_text == "yes",
    )
