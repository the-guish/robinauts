# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The start and the stop of what the composition built, in order: what the app's lifespan
runs, and what a test that needs no app runs in its place."""

from __future__ import annotations

from robinauts.controller.composition import Composed


class Lifecycle:
    def __init__(self, composed: Composed) -> None:
        self.composed = composed

    async def start(self) -> None:
        """Open the controller, then start the worker on the store it opened."""
        await self.composed.controller.open()
        self.composed.worker.start()

    async def stop(self) -> None:
        """Stop the worker, so that no turn is claimed any more, then close the controller,
        which waits for the turns this process runs."""
        await self.composed.worker.stop()
        await self.composed.controller.close()
