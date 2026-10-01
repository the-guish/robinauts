# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""``ensure_user`` creates a user once and finds it after."""

from __future__ import annotations

from aio import asyncio_test
from robinauts.controller.contract.domain import Config, Identity, StorageConfig, StorageKind
from robinauts.controller.controller import RobinautsController


@asyncio_test
async def test_ensure_user_creates_once_and_finds_after() -> None:
    controller = RobinautsController(
        Config(), storage=StorageConfig(StorageKind.IN_MEMORY), secret_for={}.get
    )
    await controller.open()
    identity = Identity("local", "me", name="Me", email="me@example.com")
    first = await controller.ensure_user(identity)
    second = await controller.ensure_user(identity)
    assert second.id == first.id
    assert (first.provider, first.subject, first.name, first.email) == (
        "local",
        "me",
        "Me",
        "me@example.com",
    )
    assert first.created_at is not None
    await controller.close()
