# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The ``[orchestration]`` table, read."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from robinauts.controller.contract.domain import ConfigError
from robinauts.orchestrator.contract.domain import MOUNT, TABLE, OrchestrationConfig

KEYS = frozenset({"code_location", "flows", "ui"})
GONE = frozenset({"command", "port", "database_url_env"})
"""Keys of the table when neorc ran beside the server, refused with the reason."""


def parse_orchestration(
    table: Mapping[str, Any], *, base: Path, default_code: Path
) -> OrchestrationConfig:
    """The ``[orchestration]`` table, with its relative paths taken from ``base``, the
    configuration file's directory. ``ConfigError`` names every problem."""
    problems = [
        f"[{TABLE}] {key}: no longer read: neorc runs inside the server, at {MOUNT}"
        for key in sorted(table.keys() & GONE)
    ]
    problems += [f"[{TABLE}] {key}: unknown key" for key in sorted(table.keys() - KEYS - GONE)]
    ui = table.get("ui", True)
    if not isinstance(ui, bool):
        problems.append(f"[{TABLE}] ui: true or false")
        ui = True
    paths: dict[str, Path] = {}
    for key in ("code_location", "flows"):
        value = table.get(key)
        if value is not None and (not isinstance(value, str) or not value):
            problems.append(f"[{TABLE}] {key}: a path")
            value = None
        if value is not None:
            paths[key] = base / value
    code_location = paths.get("code_location", default_code)
    flows = paths.get("flows", code_location / "flows")
    if problems:
        raise ConfigError("\n".join(problems))
    return OrchestrationConfig(code_location=code_location, flows=flows, ui=ui)
