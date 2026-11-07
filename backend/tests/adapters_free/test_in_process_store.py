# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The in-process store keeps the store's contract. Its one worker runs in its process, and is
handed every task, whatever worker it is stamped for."""

from __future__ import annotations

from robinauts.controller.adapters.in_process.store import InProcessStore
from robinauts.controller.ports.store import Store
from robinauts.shared_domain.domain import DEFAULT_WORKER
from util.contracts.store import MINUTE, NOW, StoreContract, store_test, user


class TestInProcessStore(StoreContract):
    async def new_store(self) -> Store:
        return InProcessStore()

    @store_test
    async def test_its_one_worker_claims_every_task(self, store: Store) -> None:
        me = user()
        await store.add_or_refresh_user(me)
        queued = [await self.queue_for(store, me, worker) for worker in (DEFAULT_WORKER, "special")]
        assert await store.wait_for_queued(NOW, 0.05, DEFAULT_WORKER) is True
        claimed = await store.claim_tasks(NOW, NOW + MINUTE, 10, DEFAULT_WORKER)
        assert sorted(k.id for k in claimed) == sorted(k.id for k in queued)
        assert await store.wait_for_queued(NOW, 0.05, DEFAULT_WORKER) is False
