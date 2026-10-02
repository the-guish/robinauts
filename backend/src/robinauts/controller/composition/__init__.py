# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Loads a configuration file, and builds a controller: the store for the storage asked, the
dispatcher that runs its turns, and the application over them."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from robinauts.controller.adapters.config_file import read_config
from robinauts.controller.adapters.dispatch import InProcessDispatcher
from robinauts.controller.adapters.memory import MemoryStore
from robinauts.controller.application.config import parse_config
from robinauts.controller.application.controller import RobinautsController
from robinauts.controller.application.engines import SecretLookup
from robinauts.controller.contract.domain import Config, StorageConfig, StorageKind
from robinauts.controller.contract.ports import Controller


def build(config: Config, *, storage: StorageConfig, secret_for: SecretLookup) -> Controller:
    if storage.kind is not StorageKind.IN_MEMORY:
        raise NotImplementedError(f"{storage.kind} storage")
    dispatcher = InProcessDispatcher()
    controller = RobinautsController(
        config, store=MemoryStore(), storage=storage, secret_for=secret_for, dispatcher=dispatcher
    )
    # Handed over here, so that no adapter imports the application.
    dispatcher.run = controller.run_turn
    return controller


def load(path: Path, environ: Mapping[str, str]) -> tuple[Config, SecretLookup]:
    return parse_config(read_config(path)), environ.get
