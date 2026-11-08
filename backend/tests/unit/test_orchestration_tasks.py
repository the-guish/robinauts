# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The handlers of the flows Robinauts ships (``orchestration/robinauts_tasks.py``): what their
requests carry. The module is loaded from its file, with no bytecode written, since
``orchestration/`` is no package of the backend's and is left as it is in the checkout."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

TASKS = Path(__file__).resolve().parents[3] / "orchestration" / "robinauts_tasks.py"


@pytest.fixture(scope="module")
def tasks() -> ModuleType:
    spec = importlib.util.spec_from_file_location("robinauts_tasks", TASKS)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    writing = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = writing
    return module


@pytest.mark.parametrize(
    ("environ", "headers"),
    [
        ({}, {}),
        ({"ROBINAUTS_API_TOKEN": ""}, {}),
        ({"ROBINAUTS_API_TOKEN": "rbt_minted"}, {"Authorization": "Bearer rbt_minted"}),
    ],
)
def test_a_request_carries_a_bearer_only_when_the_worker_was_given_an_api_token(
    tasks: ModuleType, environ: dict[str, str], headers: dict[str, str]
) -> None:
    assert tasks.api_headers(environ) == headers
