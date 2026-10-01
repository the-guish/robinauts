# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

from __future__ import annotations

from pathlib import Path

from robinauts.controller.composition import load

EXAMPLES = Path(__file__).resolve().parents[3] / "examples"
ECHO = EXAMPLES / "echo.toml"


def test_loads_the_echo_example() -> None:
    config, secret_for = load(ECHO, {"X": "y"})
    assert config.agents["echo"].engine == "echo"
    assert secret_for("X") == "y"


def test_loads_the_langchain_example() -> None:
    config, _ = load(EXAMPLES / "langchain.toml", {})
    assert config.agents["assistant"].engine == "langchain"


def test_loads_the_pydantic_ai_example() -> None:
    config, _ = load(EXAMPLES / "pydantic-ai.toml", {})
    assert config.agents["assistant"].engine == "pydantic-ai"
