# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The orchestrator's records, errors and the names a shell needs to serve it."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, MutableMapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

TABLE = "orchestration"
"""The configuration's table that turns the orchestrator on."""

MOUNT = "/neorc"
"""Where the orchestration engine's application is mounted on this server."""
UI_URL = f"{MOUNT}/ui/"
"""Where its UI is, which the interface frames."""

DEV_ONLY = (
    f"[{TABLE}]: neorc is served with no authentication, which only the local development"
    " mode allows for now: start with --dev-no-sign-in, or take the table out"
)

ASGIApp = Callable[
    [MutableMapping[str, Any], Callable[[], Awaitable[Any]], Callable[[Any], Awaitable[None]]],
    Awaitable[None],
]


class OrchestrationStartError(RuntimeError):
    """The orchestration engine's storage would not open, its flows were refused, or its worker
    could not be started."""


@dataclass(frozen=True, slots=True)
class OrchestrationConfig:
    """The ``[orchestration]`` table, read: paths are absolute."""

    code_location: Path
    flows: Path
    ui: bool = True


class Orchestrator(ABC):
    """The orchestration engine embedded in this server: its application, to mount at
    ``MOUNT``, and what runs beside it, started and stopped around the server's own."""

    @property
    @abstractmethod
    def mounted(self) -> ASGIApp:
        """The application to mount at ``MOUNT``."""

    @abstractmethod
    async def start(self) -> None:
        """Start what runs beside the server. A start that fails part way stops what it had
        started before it raises ``OrchestrationStartError``."""

    @abstractmethod
    async def stop(self) -> None:
        """Stop it. Safe to repeat."""
