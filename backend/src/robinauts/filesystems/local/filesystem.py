# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""A folder on this machine's disk.

The root is a directory the operator names, ``~/robinauts`` by default, and the folder of an
owner with an agent is ``<root>/<owner>/<agent>``, both plain names. A path is joined under
the folder's directory, **resolved**, and refused unless what it resolves to is the folder or
inside it: the segments cannot climb (``relative_path`` saw to that), so the one thing that
could lead out is a symbolic link somebody put inside, and that is what the check is for. The
check happens on every operation, on the source and the target alike.

Every operation runs in a thread, so a slow disk holds up one turn and not the worker. A write
goes to a temporary file beside its target and is then renamed over it, so a reader sees the
file before or after and never in between. Nothing here follows a pattern, deletes more than
one thing, or overwrites a file it was not asked to replace.
"""

from __future__ import annotations

import asyncio
import errno
import os
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from robinauts.filesystems.contract.domain import (
    Entry,
    ExistsError,
    IsAFolderError,
    Kind,
    NotAFolderError,
    NotEmptyError,
    NotFoundError,
    OutsideFolderError,
    PathError,
    RelativePath,
    RootUnusableError,
    TooLargeError,
    joined,
    plain,
)
from robinauts.filesystems.contract.ports import FileSystem, Folder

ROOT_MODE = 0o700
"""The root is made for the service user alone; what the operator made already is left as it
is."""


def _entry(name: str, stat: os.stat_result, is_dir: bool) -> Entry:
    modified = datetime.fromtimestamp(stat.st_mtime, UTC)
    if is_dir:
        return Entry(name, Kind.FOLDER, None, modified)
    return Entry(name, Kind.FILE, stat.st_size, modified)


class LocalFolder(Folder):
    __slots__ = ("_directory",)

    def __init__(self, directory: Path) -> None:
        self._directory = directory

    def _inside(self, path: RelativePath) -> Path:
        """The path on disk, once what it resolves to is the folder or inside it. The path
        itself is handed back, not what it resolves to: a link inside the folder is moved or
        deleted as the link it is, and read or written through."""
        root = self._directory.resolve()
        resolved = root.joinpath(*path).resolve()
        if resolved != root and root not in resolved.parents:
            raise OutsideFolderError(joined(path))
        return self._directory.joinpath(*path)

    def _file(self, path: RelativePath) -> Path:
        """The path of a file to read, move or copy: there, and not a folder."""
        if not path:
            raise PathError("Name a file: '' is the folder itself.")
        target = self._inside(path)
        if target.is_dir():
            raise IsAFolderError(joined(path))
        if not target.exists():
            raise NotFoundError(joined(path))
        return target

    def _destination(self, path: RelativePath) -> Path:
        """The path of a file to make: nothing there, and its folders made."""
        if not path:
            raise PathError("Name a file: '' is the folder itself.")
        target = self._inside(path)
        if target.exists() or target.is_symlink():
            raise ExistsError(joined(path))
        self._make_parent(path, target)
        return target

    def _make_parent(self, path: RelativePath, target: Path) -> None:
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
        except (FileExistsError, NotADirectoryError) as error:
            # A name on the way that is a file: the first one, for the message.
            for depth in range(1, len(path)):
                if not self._directory.joinpath(*path[:depth]).is_dir():
                    raise NotAFolderError(joined(path[:depth])) from error
            raise NotAFolderError(joined(path[:-1])) from error

    async def stat(self, path: RelativePath) -> Entry | None:
        def go() -> Entry | None:
            target = self._inside(path)
            try:
                stat = target.stat()
            except FileNotFoundError:
                return None
            return _entry(path[-1] if path else "", stat, target.is_dir())

        return await asyncio.to_thread(go)

    async def list(self, path: RelativePath) -> list[Entry]:
        def go() -> list[Entry]:
            target = self._inside(path)
            if not target.exists():
                if not path:
                    return []
                raise NotFoundError(joined(path))
            if not target.is_dir():
                raise NotAFolderError(joined(path))
            found = []
            with os.scandir(target) as entries:
                for entry in entries:
                    try:
                        found.append(_entry(entry.name, entry.stat(), entry.is_dir()))
                    except OSError:
                        # A link to nothing: shown by nothing, since nothing can be done with it.
                        continue
            return sorted(found, key=lambda e: e.name)

        return await asyncio.to_thread(go)

    async def read(self, path: RelativePath, max_bytes: int) -> bytes:
        def go() -> bytes:
            target = self._file(path)
            size = target.stat().st_size
            if size > max_bytes:
                raise TooLargeError(joined(path), size, max_bytes)
            with open(target, "rb") as file:
                data = file.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise TooLargeError(joined(path), len(data), max_bytes)
            return data

        return await asyncio.to_thread(go)

    async def write(self, path: RelativePath, data: bytes) -> None:
        def go() -> None:
            if not path:
                raise PathError("Name a file: '' is the folder itself.")
            target = self._inside(path)
            if target.is_dir():
                raise IsAFolderError(joined(path))
            self._make_parent(path, target)
            with tempfile.NamedTemporaryFile(
                dir=target.parent, prefix=f".{target.name}.", suffix=".writing", delete=False
            ) as temporary:
                try:
                    temporary.write(data)
                    temporary.flush()
                    os.fsync(temporary.fileno())
                    os.replace(temporary.name, target)
                except BaseException:
                    os.unlink(temporary.name)
                    raise

        await asyncio.to_thread(go)

    async def move(self, source: RelativePath, target: RelativePath) -> None:
        def go() -> None:
            moving = self._file(source)
            os.rename(moving, self._destination(target))

        await asyncio.to_thread(go)

    async def copy(self, source: RelativePath, target: RelativePath) -> None:
        def go() -> None:
            copying = self._file(source)
            destination = self._destination(target)
            with open(copying, "rb") as reading, open(destination, "xb") as writing:
                shutil.copyfileobj(reading, writing)

        await asyncio.to_thread(go)

    async def delete(self, path: RelativePath) -> Entry:
        def go() -> Entry:
            if not path:
                raise PathError("Name a file or a folder: '' is the folder itself.")
            target = self._inside(path)
            try:
                stat = target.stat()
            except FileNotFoundError as missing:
                raise NotFoundError(joined(path)) from missing
            went = _entry(path[-1], stat, target.is_dir())
            if target.is_symlink():
                # The link goes, and what it led to stays.
                target.unlink()
            elif went.kind is Kind.FOLDER:
                try:
                    target.rmdir()
                except OSError as error:
                    if error.errno in (errno.ENOTEMPTY, errno.EEXIST):
                        raise NotEmptyError(joined(path)) from error
                    raise
            else:
                target.unlink()
            return went

        return await asyncio.to_thread(go)


class LocalFileSystem(FileSystem):
    __slots__ = ("_root",)

    def __init__(self, root: Path) -> None:
        self._root = root

    @property
    def root(self) -> Path:
        return self._root

    async def setup(self) -> None:
        def go() -> None:
            try:
                self._root.mkdir(mode=ROOT_MODE, parents=True, exist_ok=True)
            except OSError as error:
                raise RootUnusableError(
                    f"the files root {self._root} cannot be made: {error.strerror or error}"
                ) from error
            if not self._root.is_dir():
                raise RootUnusableError(f"the files root {self._root} is not a folder")
            if not os.access(self._root, os.W_OK | os.X_OK):
                raise RootUnusableError(f"the files root {self._root} cannot be written")

        await asyncio.to_thread(go)

    def folder(self, owner: str, agent: str) -> Folder:
        for name in (owner, agent):
            if not plain(name):
                raise ValueError(f"{name!r} is not the name of a folder")
        return LocalFolder(self._root / owner / agent)
