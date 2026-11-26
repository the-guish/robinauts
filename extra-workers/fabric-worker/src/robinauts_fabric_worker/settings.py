# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The worker's own settings, read from its environment: where the conversations' workspaces and
the engine's state go, how Claude Code is run behind NeMo Fabric, and how many Fabric runtimes
stay alive. Every problem is reported at once, as the server reports its own.

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

PREFIX = "ROBINAUTS_FABRIC_"
WORKSPACES_VARIABLE = PREFIX + "WORKSPACES"
STATE_VARIABLE = PREFIX + "STATE"
PERMISSION_MODE_VARIABLE = PREFIX + "PERMISSION_MODE"
ALLOWED_TOOLS_VARIABLE = PREFIX + "ALLOWED_TOOLS"
DISALLOWED_TOOLS_VARIABLE = PREFIX + "DISALLOWED_TOOLS"
SYSTEM_PROMPT_VARIABLE = PREFIX + "SYSTEM_PROMPT"
MAX_TURNS_VARIABLE = PREFIX + "MAX_TURNS"
MAX_BUDGET_VARIABLE = PREFIX + "MAX_BUDGET_USD"
EFFORT_VARIABLE = PREFIX + "EFFORT"
MAX_RUNTIMES_VARIABLE = PREFIX + "MAX_RUNTIMES"
IDLE_SECONDS_VARIABLE = PREFIX + "IDLE_SECONDS"
FLOWS_VARIABLE = "ROBINAUTS_FLOWS_DIR"

DEFAULT_WORKSPACES = "/workspace"
DEFAULT_STATE = "/var/lib/robinauts-fabric"
DEFAULT_PERMISSION_MODE = "dontAsk"
DEFAULT_ALLOWED_TOOLS = ("Read", "Edit", "Write", "Glob", "Grep", "Bash", "WebFetch", "WebSearch")
"""What runs without asking, by default: Claude Code's own tools. Nobody can answer a question
from the chat, so ``dontAsk`` denies the rest with a message the model sees."""
DEFAULT_DISALLOWED_TOOLS = ("AskUserQuestion", "ExitPlanMode")
"""The tools that wait for a person: there is none at the other end."""
DEFAULT_MAX_RUNTIMES = 4
DEFAULT_IDLE_SECONDS = 900

PERMISSION_MODES = ("default", "acceptEdits", "plan", "bypassPermissions", "dontAsk", "auto")
EFFORTS = ("low", "medium", "high", "xhigh", "max")


class SettingsError(Exception):
    """The environment is not this worker's: every problem, one per line."""


class SystemPromptMode(StrEnum):
    PRESET = "preset"
    """Claude Code's own system prompt, with the agent's appended."""
    CUSTOM = "custom"
    """The agent's system prompt alone."""


@dataclass(frozen=True, slots=True)
class Settings:
    workspaces: Path
    """Where each conversation gets a directory of its own, Claude Code's ``cwd``."""
    state: Path
    """The engine's own state: each conversation's turns, and Fabric's runtime folders."""
    permission_mode: str
    allowed_tools: tuple[str, ...]
    disallowed_tools: tuple[str, ...]
    system_prompt: SystemPromptMode
    max_turns: int | None
    max_budget_usd: float | None
    effort: str | None
    max_runtimes: int
    """How many Fabric runtimes, each a Claude Code process, stay alive between turns."""
    idle_seconds: int
    """How long a runtime nobody used stays alive before the next turn stops it."""


def _names(value: str) -> tuple[str, ...]:
    return tuple(name.strip() for name in value.split(",") if name.strip())


def _positive_int(text: str) -> int | None:
    return int(text) if text.isdigit() and int(text) > 0 else None


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
    if given(FLOWS_VARIABLE):
        problems.append(
            f"{FLOWS_VARIABLE} is set: this worker makes no worktrees of the flows' repositories"
            " yet; the Claude Code worker does"
        )

    permission_mode = given(PERMISSION_MODE_VARIABLE, DEFAULT_PERMISSION_MODE)
    if permission_mode not in PERMISSION_MODES:
        problems.append(
            f"{PERMISSION_MODE_VARIABLE} {permission_mode!r} is not one of {PERMISSION_MODES}"
        )

    allowed = _names(given(ALLOWED_TOOLS_VARIABLE, ",".join(DEFAULT_ALLOWED_TOOLS)))
    disallowed = _names(given(DISALLOWED_TOOLS_VARIABLE, ",".join(DEFAULT_DISALLOWED_TOOLS)))

    prompt_text = given(SYSTEM_PROMPT_VARIABLE, SystemPromptMode.PRESET.value)
    system_prompt = SystemPromptMode.PRESET
    try:
        system_prompt = SystemPromptMode(prompt_text)
    except ValueError:
        problems.append(f"{SYSTEM_PROMPT_VARIABLE} must be 'preset' or 'custom'")

    max_turns = None
    if text := given(MAX_TURNS_VARIABLE):
        max_turns = _positive_int(text)
        if max_turns is None:
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

    max_runtimes = _positive_int(given(MAX_RUNTIMES_VARIABLE, str(DEFAULT_MAX_RUNTIMES)))
    if max_runtimes is None:
        problems.append(f"{MAX_RUNTIMES_VARIABLE} must be a positive whole number")
    idle_seconds = _positive_int(given(IDLE_SECONDS_VARIABLE, str(DEFAULT_IDLE_SECONDS)))
    if idle_seconds is None:
        problems.append(f"{IDLE_SECONDS_VARIABLE} must be a positive whole number of seconds")

    if problems:
        raise SettingsError("\n".join(problems))
    assert max_runtimes is not None
    assert idle_seconds is not None
    return Settings(
        workspaces=Path(given(WORKSPACES_VARIABLE, DEFAULT_WORKSPACES)),
        state=Path(given(STATE_VARIABLE, DEFAULT_STATE)),
        permission_mode=permission_mode,
        allowed_tools=allowed,
        disallowed_tools=disallowed,
        system_prompt=system_prompt,
        max_turns=max_turns,
        max_budget_usd=max_budget,
        effort=effort,
        max_runtimes=max_runtimes,
        idle_seconds=idle_seconds,
    )
