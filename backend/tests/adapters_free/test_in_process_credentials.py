# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The in-process credentials keep the credentials' contract."""

from __future__ import annotations

from robinauts.controller.adapters.in_process.credentials import InProcessCredentials
from robinauts.controller.adapters.in_process.store import InProcessStore
from robinauts.controller.contract.ports import Credentials
from robinauts.controller.ports.store import Store
from util.contracts.credentials import CredentialsContract


class TestInProcessCredentials(CredentialsContract):
    async def new_credentials(self) -> tuple[Credentials, Store]:
        store = InProcessStore()
        return InProcessCredentials(store), store
