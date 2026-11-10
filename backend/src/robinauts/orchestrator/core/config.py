# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The flows folder's settings, ``flows.toml``, read."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from robinauts.controller.contract.domain import ConfigError
from robinauts.orchestrator.contract.domain import SETTINGS_FILE, OrchestrationConfig

KEYS = frozenset({"code_location", "ui"})


def parse_flows_settings(
    settings: Mapping[str, Any], *, folder: Path, default_code: Path
) -> OrchestrationConfig:
    """The flows in ``folder``, with the settings of its ``flows.toml``: a relative
    ``code_location`` starts at the folder, and is ``default_code`` when it names none.
    ``ConfigError`` names every problem."""
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
    if problems:
        raise ConfigError("\n".join(problems))
    return OrchestrationConfig(code_location=code_location, flows=folder, ui=ui)
