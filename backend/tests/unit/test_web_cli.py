# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

from __future__ import annotations

import pytest

from robinauts.web.cli import run


def test_version_prints_the_version(capsys: pytest.CaptureFixture[str]) -> None:
    run(["version"])
    assert capsys.readouterr().out.strip()
