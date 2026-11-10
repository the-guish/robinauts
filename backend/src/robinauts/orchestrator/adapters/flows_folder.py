# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The flows folder's settings file and form files, read from the disk."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from robinauts.controller.contract.domain import ConfigError
from robinauts.orchestrator.contract.domain import FORM_SUFFIX, SETTINGS_FILE


def read_settings(folder: Path) -> dict[str, Any]:
    """The tables of the folder's ``flows.toml``; none when it has no such file.
    ``ConfigError`` for a file that is not TOML."""
    path = folder / SETTINGS_FILE
    try:
        return _read(path)
    except FileNotFoundError:
        return {}


def read_forms(folder: Path) -> dict[str, dict[str, Any]]:
    """By flow name, the tables of each ``<flow name>.form.toml`` in the folder.
    ``ConfigError`` for a file that is not TOML."""
    return {
        path.name.removesuffix(FORM_SUFFIX): _read(path)
        for path in sorted(folder.glob(f"*{FORM_SUFFIX}"))
    }


def _read(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as file:
            return tomllib.load(file)
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"{path}: not TOML: {error}") from error
