# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The file system port: a folder, and the file system that hands out folders.

A ``Folder`` is the whole reach of whoever holds it. Every operation takes a ``RelativePath``,
made by ``relative_path``, so an adapter is never handed a path it has to make sense of: the
segments name something inside the folder, or the path was refused already. What an adapter
adds is the one thing a path cannot say and the storage can: a link that leads out, which it
refuses with ``OutsideFolderError``.

A ``FileSystem`` is the place every folder is kept: a directory, a bucket, a drive. It hands
out the folder of one owner with one agent, named by two plain names, and creates nothing
until something is written there.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from robinauts.filesystems.contract.domain import Entry, RelativePath


class Folder(ABC):
    """One folder, bound: nothing outside it can be named through it.

    The refusals are the contract's errors (``filesystems.contract.domain``): ``NotFoundError``
    for a path with nothing at it, ``IsAFolderError`` for a folder where a file was expected and
    ``NotAFolderError`` for the reverse, ``ExistsError`` for a target already there,
    ``NotEmptyError`` for a folder that cannot go, ``TooLargeError`` for a file past the size
    asked, ``OutsideFolderError`` for a path a link leads out through. ``()`` names the folder
    itself, and is taken by ``stat`` and ``list`` alone: the others raise ``PathError`` for it.
    """

    __slots__ = ()

    @abstractmethod
    async def stat(self, path: RelativePath) -> Entry | None:
        """What is at the path, or ``None``. The folder itself, while nothing was written in
        it yet, is ``None`` too."""
        raise NotImplementedError

    @abstractmethod
    async def list(self, path: RelativePath) -> list[Entry]:
        """What the folder at the path holds, by name. The folder itself, while nothing was
        written in it yet, holds nothing; any other path with nothing at it is
        ``NotFoundError``."""
        raise NotImplementedError

    @abstractmethod
    async def read(self, path: RelativePath, max_bytes: int) -> bytes:
        """The file, whole. ``TooLargeError`` for one over ``max_bytes``, read or not."""
        raise NotImplementedError

    @abstractmethod
    async def write(self, path: RelativePath, data: bytes) -> None:
        """The file, created or replaced whole, its folders made as needed. A reader sees the
        file before or after, never half written."""
        raise NotImplementedError

    @abstractmethod
    async def move(self, source: RelativePath, target: RelativePath) -> None:
        """A file, to a path with nothing at it, its folders made as needed."""
        raise NotImplementedError

    @abstractmethod
    async def copy(self, source: RelativePath, target: RelativePath) -> None:
        """A file, to a path with nothing at it, its folders made as needed."""
        raise NotImplementedError

    @abstractmethod
    async def delete(self, path: RelativePath) -> Entry:
        """A file, or an empty folder: what went."""
        raise NotImplementedError


class FileSystem(ABC):
    """Where the folders are kept, and how one is bound."""

    __slots__ = ()

    @abstractmethod
    async def setup(self) -> None:
        """Make the place ready, safe to repeat: ``RootUnusableError`` when it cannot be."""
        raise NotImplementedError

    @abstractmethod
    def folder(self, owner: str, agent: str) -> Folder:
        """The folder of that owner with that agent, bound; made when first written to.
        ``ValueError`` for a name that is not ``plain``."""
        raise NotImplementedError
