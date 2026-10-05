# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The worker: what runs turns, through the controller that holds them."""

from __future__ import annotations

from robinauts.controller.contract.ports import Controller


class Worker:
    def __init__(self, controller: Controller) -> None:
        self._controller = controller
