# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

from __future__ import annotations

from pathlib import Path

import pytest

from robinauts.controller.composition import load

EXAMPLES = Path(__file__).resolve().parents[3] / "examples"


@pytest.mark.parametrize(
    ("example", "agent", "engine"),
    [
        ("echo.toml", "echo", "echo"),
        ("langchain.toml", "assistant", "langchain"),
        ("pydantic-ai.toml", "assistant", "pydantic-ai"),
        ("kataloop.toml", "assistant", "kataloop"),
    ],
)
def test_loads_the_example(example: str, agent: str, engine: str) -> None:
    config, secret_for = load(EXAMPLES / example, {"X": "y"})
    assert config.agents[agent].engine == engine
    assert secret_for("X") == "y"
