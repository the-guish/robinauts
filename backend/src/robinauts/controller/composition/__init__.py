# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Builds a controller: the store for the storage asked, and the application over it."""

from __future__ import annotations

from robinauts.controller.adapters.memory import MemoryStore
from robinauts.controller.application.controller import RobinautsController
from robinauts.controller.application.engines import SecretLookup
from robinauts.controller.contract.domain import Config, StorageConfig, StorageKind
from robinauts.controller.contract.ports import Controller


def build(config: Config, *, storage: StorageConfig, secret_for: SecretLookup) -> Controller:
    if storage.kind is not StorageKind.IN_MEMORY:
        raise NotImplementedError(f"{storage.kind} storage")
    return RobinautsController(config, store=MemoryStore(), storage=storage, secret_for=secret_for)
