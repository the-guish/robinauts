# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The folder in memory the tools are tested over keeps the folder's contract."""

from __future__ import annotations

from robinauts.filesystems.contract.ports import Folder
from util.contracts.folder import FolderContract
from util.memory_folder import MemoryFolder


class TestMemoryFolder(FolderContract):
    async def new_folder(self) -> Folder:
        return MemoryFolder()
