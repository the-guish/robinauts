# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

from __future__ import annotations

from pathlib import Path

from robinauts.controller.composition import load

ECHO = Path(__file__).resolve().parents[3] / "examples" / "echo.toml"


def test_loads_the_echo_example() -> None:
    config, secret_for = load(ECHO, {"X": "y"})
    assert config.agents["echo"].engine == "echo"
    assert secret_for("X") == "y"
