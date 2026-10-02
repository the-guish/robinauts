# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The in-memory credentials keep the credentials' contract."""

from __future__ import annotations

from contracts.credentials import CredentialsContract
from robinauts.controller.adapters.memory.credentials import MemoryCredentials
from robinauts.controller.adapters.memory.store import MemoryStore
from robinauts.controller.contract.ports import Credentials
from robinauts.controller.ports.store import Store


class TestMemoryCredentials(CredentialsContract):
    async def new_credentials(self) -> tuple[Credentials, Store]:
        store = MemoryStore()
        return MemoryCredentials(store), store
