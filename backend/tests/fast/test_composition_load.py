# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

from __future__ import annotations

from pathlib import Path

import pytest

from robinauts.cli.composition import load
from robinauts.shared_domain.domain import FilesConfig, FilesKind

EXAMPLES = Path(__file__).resolve().parents[3] / "examples"


@pytest.mark.parametrize(
    ("example", "agent", "engine"),
    [
        ("echo.toml", "echo", "echo"),
        ("langchain.toml", "assistant", "langchain"),
        ("pydantic-ai.toml", "assistant", "pydantic-ai"),
        ("basic-edit.toml", "notes", "echo"),
    ],
)
def test_loads_the_example(example: str, agent: str, engine: str) -> None:
    config, secret_for = load(EXAMPLES / example, {"X": "y"})
    assert config.agents[agent].engine == engine
    assert secret_for("X") == "y"


def test_the_notes_example_keeps_files_at_the_default_place() -> None:
    config, _ = load(EXAMPLES / "basic-edit.toml", {})
    assert config.agents["notes"].tools == ("basic_edit",)
    assert config.files == FilesConfig(FilesKind.LOCAL, "~/robinauts")
