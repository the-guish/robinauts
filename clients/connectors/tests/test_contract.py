# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The contract of ``connector.py``, as far as stubs can keep it: every connector the distribution
registers is found by its entry point, under its own name, declaring how it receives; a mode it does
not declare is refused; and the import rules of ``pyproject.toml`` hold."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Mapping
from importlib.metadata import entry_points
from pathlib import Path
from typing import ClassVar, Self

import pytest

from robinauts_connectors.connector import Connector, Inbox
from robinauts_connectors.domain import AllowList, Mode, UnsupportedModeError
from robinauts_connectors.registry import GROUP

ROOT = Path(__file__).resolve().parents[1]


def registered() -> dict[str, type[Connector]]:
    return {point.name: point.load() for point in entry_points(group=GROUP)}


def test_the_distribution_registers_slack_and_telegram_under_their_own_names() -> None:
    connectors = registered()
    assert {"slack", "telegram"} <= connectors.keys()
    for name, connector in connectors.items():
        assert issubclass(connector, Connector)
        assert connector.name == name
        assert connector.modes
        assert connector.modes <= set(Mode)


class ConnectOnly(Connector):
    name: ClassVar[str] = "connect-only"
    modes: ClassVar[frozenset[Mode]] = frozenset({Mode.CONNECT})

    @classmethod
    def from_environment(cls, environ: Mapping[str, str]) -> Self:
        return cls()

    @property
    def allowed(self) -> AllowList:
        return AllowList(everyone=True)


class NoInbox(Inbox):
    def submit(self, message: object, reply: object) -> None:
        raise AssertionError("nothing is submitted here")


def test_a_mode_a_connector_does_not_declare_is_refused() -> None:
    with pytest.raises(UnsupportedModeError) as refused:
        ConnectOnly.from_environment({}).webhook(NoInbox())
    assert refused.value.mode is Mode.SERVE


def test_the_import_rules_are_kept() -> None:
    result = subprocess.run(
        [str(Path(sys.executable).parent / "lint-imports"), "--no-cache"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "0 broken" in result.stdout, result.stdout
