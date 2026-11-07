# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""A ``Folder`` in memory, for the tests of what is built on one: the contract's rules over two
dictionaries, with no disk. It keeps the contract (``tests/unit/test_memory_folder.py``), so a
tool tested over it is tested over what every adapter promises."""

from __future__ import annotations

from datetime import UTC, datetime

from robinauts.filesystems.contract.domain import (
    Entry,
    ExistsError,
    IsAFolderError,
    Kind,
    NotAFolderError,
    NotEmptyError,
    NotFoundError,
    PathError,
    RelativePath,
    TooLargeError,
    joined,
)
from robinauts.filesystems.contract.ports import Folder

WRITTEN_AT = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


class MemoryFolder(Folder):
    def __init__(self) -> None:
        self.files: dict[RelativePath, bytes] = {}
        self.folders: set[RelativePath] = set()
        """Every folder written in, the folder itself as ``()`` once anything was."""

    def _kind(self, path: RelativePath) -> Kind | None:
        if path in self.files:
            return Kind.FILE
        if path in self.folders:
            return Kind.FOLDER
        return None

    def _entry(self, path: RelativePath) -> Entry | None:
        kind = self._kind(path)
        if kind is None:
            return None
        name = path[-1] if path else ""
        size = len(self.files[path]) if kind is Kind.FILE else None
        return Entry(name, kind, size, WRITTEN_AT)

    def _file(self, path: RelativePath) -> bytes:
        if not path:
            raise PathError("Name a file: '' is the folder itself.")
        kind = self._kind(path)
        if kind is Kind.FOLDER:
            raise IsAFolderError(joined(path))
        if kind is None:
            raise NotFoundError(joined(path))
        return self.files[path]

    def _destination(self, path: RelativePath) -> None:
        if not path:
            raise PathError("Name a file: '' is the folder itself.")
        if self._kind(path) is not None:
            raise ExistsError(joined(path))
        self._make_parents(path)

    def _make_parents(self, path: RelativePath) -> None:
        for depth in range(len(path)):
            above = path[:depth]
            if above in self.files:
                raise NotAFolderError(joined(above))
            self.folders.add(above)

    async def stat(self, path: RelativePath) -> Entry | None:
        return self._entry(path)

    async def list(self, path: RelativePath) -> list[Entry]:
        kind = self._kind(path)
        if kind is Kind.FILE:
            raise NotAFolderError(joined(path))
        if kind is None:
            if path:
                raise NotFoundError(joined(path))
            return []
        inside = {
            p[: len(path) + 1] for p in (*self.files, *self.folders) if p[: len(path)] == path
        }
        names = sorted(p[-1] for p in inside if len(p) == len(path) + 1)
        return [entry for name in names if (entry := self._entry((*path, name))) is not None]

    async def read(self, path: RelativePath, max_bytes: int) -> bytes:
        data = self._file(path)
        if len(data) > max_bytes:
            raise TooLargeError(joined(path), len(data), max_bytes)
        return data

    async def write(self, path: RelativePath, data: bytes) -> None:
        if not path:
            raise PathError("Name a file: '' is the folder itself.")
        if self._kind(path) is Kind.FOLDER:
            raise IsAFolderError(joined(path))
        self._make_parents(path)
        self.files[path] = bytes(data)

    async def move(self, source: RelativePath, target: RelativePath) -> None:
        data = self._file(source)
        self._destination(target)
        del self.files[source]
        self.files[target] = data

    async def copy(self, source: RelativePath, target: RelativePath) -> None:
        data = self._file(source)
        self._destination(target)
        self.files[target] = data

    async def delete(self, path: RelativePath) -> Entry:
        if not path:
            raise PathError("Name a file or a folder: '' is the folder itself.")
        went = self._entry(path)
        if went is None:
            raise NotFoundError(joined(path))
        if went.kind is Kind.FILE:
            del self.files[path]
            return went
        if any(p[: len(path)] == path and p != path for p in (*self.files, *self.folders)):
            raise NotEmptyError(joined(path))
        self.folders.discard(path)
        return went
