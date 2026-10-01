# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any


def read_config(path: Path) -> dict[str, Any]:
    with path.open("rb") as file:
        return tomllib.load(file)
