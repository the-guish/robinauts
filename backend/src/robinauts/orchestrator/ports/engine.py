# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The orchestration engine, as the orchestrator's application uses it."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from typing import Any


class FlowEngine(ABC):
    """Where a run of a flow starts, and where the flows are."""

    @abstractmethod
    async def has_flow(self, name: str, *, credential: str | None) -> bool:
        """Whether the engine holds a flow by that name, asked with ``credential`` as a
        bearer: the person's own, so the answer is about the flows that person may read. A
        text that cannot name a flow names none."""

    @abstractmethod
    async def start_run(
        self, flow: str, inputs: Mapping[str, Any], *, credential: str | None
    ) -> str:
        """Start a run of the latest version of ``flow`` and return its id, asked with
        ``credential`` as a bearer: the person's own. ``NoSuchFlowError`` for a flow the engine
        does not hold, ``InvalidInputsError`` for inputs it refuses."""
