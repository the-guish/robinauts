# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The in-process store keeps the store's contract."""

from __future__ import annotations

from robinauts.controller.adapters.in_process.store import InProcessStore
from robinauts.controller.ports.store import Store
from util.contracts.store import StoreContract


class TestInProcessStore(StoreContract):
    async def new_store(self) -> Store:
        return InProcessStore()
