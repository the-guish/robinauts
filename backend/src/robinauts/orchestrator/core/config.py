# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The flows folder's settings, ``flows.toml``, read."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from robinauts.controller.contract.domain import ConfigError
from robinauts.orchestrator.contract.domain import (
    SETTINGS_FILE,
    FormSpec,
    OrchestrationConfig,
    Repository,
)

KEYS = frozenset({"code_location", "ui", "repositories"})
REPOSITORY_KEYS = frozenset({"url", "branches"})


def parse_flows_settings(
    settings: Mapping[str, Any],
    *,
    folder: Path,
    default_code: Path,
    forms: Mapping[str, FormSpec] | None = None,
) -> OrchestrationConfig:
    """The flows in ``folder``, with the settings of its ``flows.toml``: a relative
    ``code_location`` starts at the folder, and is ``default_code`` when it names none.
    ``forms`` are the folder's, already read. ``ConfigError`` names every problem."""
    problems = [f"{SETTINGS_FILE} {key}: unknown key" for key in sorted(settings.keys() - KEYS)]
    ui = settings.get("ui", True)
    if not isinstance(ui, bool):
        problems.append(f"{SETTINGS_FILE} ui: true or false")
        ui = True
    code_location = default_code
    value = settings.get("code_location")
    if value is not None:
        if isinstance(value, str) and value:
            code_location = folder / value
        else:
            problems.append(f"{SETTINGS_FILE} code_location: a path")
    repositories = _repositories(settings.get("repositories", {}), problems)
    if problems:
        raise ConfigError("\n".join(problems))
    return OrchestrationConfig(
        code_location=code_location,
        flows=folder,
        ui=ui,
        repositories=repositories,
        forms=dict(forms or {}),
    )


def _repositories(tables: Any, problems: list[str]) -> tuple[Repository, ...]:
    where = f"{SETTINGS_FILE} [repositories]"
    if not isinstance(tables, Mapping):
        problems.append(f"{where}: a table of repositories by id")
        return ()
    found = []
    for id_, table in tables.items():
        here = f"{where}.{id_}"
        if not isinstance(table, Mapping):
            problems.append(f"{here}: a table with url and branches")
            continue
        problems += [f"{here} {key}: unknown key" for key in sorted(table.keys() - REPOSITORY_KEYS)]
        url = table.get("url")
        if not isinstance(url, str) or not url:
            problems.append(f"{here} url: the repository's URL")
            continue
        branches = table.get("branches")
        if (
            not isinstance(branches, list)
            or not branches
            or not all(isinstance(b, str) and b and "@" not in b for b in branches)
        ):
            problems.append(f"{here} branches: a list of branch names, none empty or with an @")
            continue
        found.append(Repository(id=id_, url=url, branches=tuple(branches)))
    return tuple(found)
