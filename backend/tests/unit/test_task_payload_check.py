# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""A question's task payload, checked as it comes in (``controller/core/task_payloads.py``)."""

from __future__ import annotations

import pytest

from robinauts.controller.contract.domain import InvalidValueError
from robinauts.controller.core.task_payloads import (
    MAX_TASK_KEY_CHARS,
    MAX_TASK_PAYLOAD_BYTES,
    check_task_payload,
)

PAYLOAD = {"task_key": "code_task/1", "repositories": {"app": {"branch": "develop"}}}


@pytest.mark.parametrize(
    ("given", "kept"),
    [
        (None, None),
        (PAYLOAD, PAYLOAD),
        ({"task_key": "x"}, {"task_key": "x"}),
    ],
)
def test_a_payload_with_its_task_key_is_kept_as_it_came(
    given: object, kept: dict[str, object] | None
) -> None:
    assert check_task_payload(given) == kept


@pytest.mark.parametrize(
    ("given", "said"),
    [
        (["task_key", "x"], "a task_payload is a JSON object"),
        ("code_task/1", "a task_payload is a JSON object"),
        ({}, "names its kind in task_key"),
        ({"task_key": ""}, "names its kind in task_key"),
        ({"task_key": "  "}, "names its kind in task_key"),
        ({"task_key": 1}, "names its kind in task_key"),
        ({"task_key": "k" * (MAX_TASK_KEY_CHARS + 1)}, "task_key is over 200 chars"),
        ({"task_key": "x", "n": float("nan")}, "a task_payload is not JSON"),
        ({"task_key": "x", "blob": "é" * MAX_TASK_PAYLOAD_BYTES}, "is over 65536 bytes"),
    ],
)
def test_a_payload_that_is_not_a_task_is_refused_by_name(given: object, said: str) -> None:
    with pytest.raises(InvalidValueError, match=said):
        check_task_payload(given)
