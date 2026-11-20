# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The branches of the repositories the flows' settings name, as the orchestrator's application
reads them."""

from __future__ import annotations

from abc import ABC, abstractmethod

from robinauts.orchestrator.contract.domain import Repository


class BranchNames(ABC):
    """Where the branches of a repository are read."""

    @abstractmethod
    async def listed(self, repository: Repository) -> tuple[str, ...]:
        """Every branch of ``repository``, by name, as read at most a short while ago.
        ``BranchesUnreadableError``."""

    @abstractmethod
    async def exists(self, repository: Repository, branch: str) -> bool:
        """Whether ``repository`` has ``branch`` now: answered from a read while it is fresh,
        and asked of its server otherwise, so a branch pushed since the last read exists and
        one deleted long ago does not. ``BranchesUnreadableError``."""
