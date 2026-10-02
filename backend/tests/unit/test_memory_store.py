# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The in-memory store keeps the store's contract."""

from __future__ import annotations

from contracts.store import StoreContract
from robinauts.controller.adapters.memory.store import MemoryStore
from robinauts.controller.ports.store import Store


class TestMemoryStore(StoreContract):
    async def new_store(self) -> Store:
        return MemoryStore()
