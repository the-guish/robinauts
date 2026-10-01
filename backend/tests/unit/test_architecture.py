# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The layer rules of docs/architecture/rules.md, as backend/pyproject.toml states them, hold."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_the_contracts_are_kept() -> None:
    result = subprocess.run(
        [
            str(Path(sys.executable).parent / "lint-imports"),
            "--no-cache",
            "--config",
            "pyproject.toml",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "0 broken" in result.stdout, result.stdout
