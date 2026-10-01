# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

from __future__ import annotations

from pathlib import Path

from robinauts.controller.adapters.config_file import read_config


def test_reads_the_four_tables(tmp_path: Path) -> None:
    path = tmp_path / "robinauts.toml"
    path.write_text(
        '[model_providers.p]\nkind = "anthropic"\n'
        '[models.m]\nprovider = "p"\n'
        '[tool_servers.t]\nurl = "https://t"\n'
        '[agents.a]\nmodel = "m"\n'
    )
    raw = read_config(path)
    assert {table: list(ids) for table, ids in raw.items()} == {
        "model_providers": ["p"],
        "models": ["m"],
        "tool_servers": ["t"],
        "agents": ["a"],
    }
