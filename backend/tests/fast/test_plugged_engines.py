# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""An engine a package installed beside this one registers under the ``robinauts.engines``
entry-point group is one this build's worker can run, by that name; one that cannot be
imported is left out, like a shipped engine whose extra is missing, and one that takes a
shipped engine's name does not take its place.
"""

from __future__ import annotations

from importlib.metadata import EntryPoint

import pytest

from robinauts.agent_engines.contract import ports
from robinauts.agent_engines.echo_engine import init_echo


def test_a_plugged_engine_is_installed_by_its_name(monkeypatch: pytest.MonkeyPatch) -> None:
    def registered(group: str) -> list[EntryPoint]:
        assert group == ports.ENTRY_POINTS
        return [
            EntryPoint("plugged", "robinauts.agent_engines.echo_engine:init_echo", group),
            EntryPoint("echo", "robinauts.agent_engines.langchain_engine:init_langchain", group),
            EntryPoint("broken", "robinauts.nowhere:init", group),
        ]

    monkeypatch.setattr(ports, "entry_points", registered)
    found = ports.installed()
    assert found["plugged"] is init_echo
    assert found["echo"] is init_echo
    assert "broken" not in found
