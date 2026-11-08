# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""What tells a task's repeat from another request under the same idempotency key."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping


def request_digest(agent: str, model: str | None, prompt: str, labels: Mapping[str, str]) -> str:
    """The SHA-256 hex of a task's request as it was sent: ``model`` ``None`` and the agent's
    default model are two requests, and the order of the labels is none of its business."""
    request = [agent, model, prompt, sorted(labels.items())]
    # ASCII escapes: a lone surrogate in a prompt has a spelling, and the same one every time.
    text = json.dumps(request, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("ascii")).hexdigest()
