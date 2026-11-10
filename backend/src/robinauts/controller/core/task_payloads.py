# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""A question's task payload, checked as it comes in: structured data beside the text, for the
engine (``docs/specs/wire.md``, "Task payloads"). Untyped past its ``task_key``: what a task's
payload holds is the business of the flow that sends it and the engine that reads it."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from robinauts.controller.contract.domain import InvalidValueError
from robinauts.shared_domain.domain import TASK_KEY

MAX_TASK_PAYLOAD_BYTES = 64 * 1024
"""The most a task payload may weigh, as compact JSON in UTF-8: data, not documents."""
MAX_TASK_KEY_CHARS = 200


def check_task_payload(value: object) -> dict[str, Any] | None:
    """``value`` as a question's task payload, or ``None`` for none. ``InvalidValueError``
    for one that is not a JSON object, has no ``task_key`` string, or is too large."""
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise InvalidValueError("a task_payload is a JSON object")
    key = value.get(TASK_KEY)
    if not isinstance(key, str) or not key.strip():
        raise InvalidValueError(f"a task_payload names its kind in {TASK_KEY}, a string")
    if len(key) > MAX_TASK_KEY_CHARS:
        raise InvalidValueError(f"a task_payload's {TASK_KEY} is over {MAX_TASK_KEY_CHARS} chars")
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError) as refused:
        raise InvalidValueError(f"a task_payload is not JSON: {refused}") from None
    if len(encoded.encode()) > MAX_TASK_PAYLOAD_BYTES:
        raise InvalidValueError(f"a task_payload is over {MAX_TASK_PAYLOAD_BYTES} bytes")
    return dict(value)
