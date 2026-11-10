# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The orchestration engine, as the orchestrator's application uses it."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from typing import Any


class FlowEngine(ABC):
    """Where a run of a flow starts."""

    @abstractmethod
    async def start_run(self, flow: str, inputs: Mapping[str, Any]) -> str:
        """Start a run of the latest version of ``flow`` and return its id.
        ``NoSuchFlowError`` for a flow the engine does not hold, ``InvalidInputsError`` for
        inputs it refuses."""
