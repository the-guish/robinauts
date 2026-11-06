# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Where the engines keep their memory: the handle an engine's storage takes.

It is the engines' alone, apart from the core's records, which a worker reaches through its
``TurnBroker``. On PostgreSQL it is a pool of the worker's own, which the engines' tables are
read and written through; the core's tables never are.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class EngineMemory(ABC):
    @abstractmethod
    async def open(self) -> object | None:
        """What the engines take as their storage's handle: a PostgreSQL pool, or ``None``."""
        raise NotImplementedError

    @abstractmethod
    async def close(self) -> None:
        """Release what ``open`` took."""
        raise NotImplementedError
