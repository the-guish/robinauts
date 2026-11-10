# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The flows folder's settings file, read from the disk."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from robinauts.controller.contract.domain import ConfigError
from robinauts.orchestrator.contract.domain import SETTINGS_FILE


def read_settings(folder: Path) -> dict[str, Any]:
    """The tables of the folder's ``flows.toml``; none when it has no such file.
    ``ConfigError`` for a file that is not TOML."""
    path = folder / SETTINGS_FILE
    try:
        with path.open("rb") as file:
            return tomllib.load(file)
    except FileNotFoundError:
        return {}
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"{path}: not TOML: {error}") from error
