# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The PostgreSQL credentials keep the credentials' contract, over the store's pool."""

from __future__ import annotations

from contracts.credentials import CredentialsContract
from controller_db import TemporarySchema, requires_postgres
from robinauts.controller.adapters.postgres.credentials import PostgresCredentials
from robinauts.controller.adapters.postgres.schema import create_schema
from robinauts.controller.adapters.postgres.store import PostgresStore
from robinauts.controller.contract.ports import Credentials
from robinauts.controller.ports.store import Store

pytestmark = requires_postgres


class TestPostgresCredentials(CredentialsContract):
    async def new_credentials(self) -> tuple[Credentials, Store]:
        schema = TemporarySchema()
        pool = await schema.open()
        await create_schema(pool)
        store = PostgresStore(pool)
        self.__dict__.setdefault("schemas", {})[id(store)] = schema
        return PostgresCredentials(store), store

    async def close_credentials(self, credentials: Credentials, store: Store) -> None:
        assert isinstance(store, PostgresStore)
        await store.close()
        await self.__dict__["schemas"].pop(id(store)).close()
