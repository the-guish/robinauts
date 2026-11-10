# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

from __future__ import annotations

import shutil
from collections.abc import Iterator

import pytest

from tests.github_stand_in import Server, serving

NEEDS_GIT = pytest.mark.skipif(
    not (shutil.which("git") and shutil.which("openssl")), reason="needs git and openssl"
)
"""The mark of the tests that run git against the stand-in."""


@pytest.fixture(scope="session")
def stand_in(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Server]:
    with serving(tmp_path_factory.mktemp("github"), tmp_path_factory.mktemp("tls")) as server:
        yield server


@pytest.fixture
def server(stand_in: Server) -> Iterator[Server]:
    """The stand-in, asking for no token and having seen nothing."""
    stand_in.seen.clear()
    stand_in.token = None
    yield stand_in
