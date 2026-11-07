# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""What every ``Folder`` must do: the rules of ``docs/specs/files.md``, "The folder".

Subclass ``FolderContract`` and override ``new_folder``, which returns a folder with nothing in
it yet. A test here names paths as the tools do, by their segments, and asks of the folder
only what the port offers: an adapter that passes is one the tools can be given.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any

import pytest

from robinauts.filesystems.contract.domain import (
    Entry,
    ExistsError,
    IsAFolderError,
    Kind,
    NotAFolderError,
    NotEmptyError,
    NotFoundError,
    PathError,
    TooLargeError,
)
from robinauts.filesystems.contract.ports import Folder

PLENTY = 1_000_000


def folder_test(test: Callable[..., Coroutine[Any, Any, None]]) -> Callable[..., None]:
    """Run the test on a loop of its own, over a new folder."""

    def run(self: Any) -> None:
        async def go() -> None:
            await test(self, await self.new_folder())

        asyncio.run(go())

    run.__name__, run.__qualname__ = test.__name__, test.__qualname__
    run.__doc__, run.__module__ = test.__doc__, test.__module__
    return run


def names(entries: list[Entry]) -> list[tuple[str, Kind]]:
    return [(e.name, e.kind) for e in entries]


class FolderContract:
    async def new_folder(self) -> Folder:
        raise NotImplementedError

    @folder_test
    async def test_a_folder_nothing_was_written_in_holds_nothing(self, folder: Folder) -> None:
        assert await folder.stat(()) is None
        assert await folder.list(()) == []
        assert await folder.stat(("a.md",)) is None
        with pytest.raises(NotFoundError):
            await folder.list(("a",))

    @folder_test
    async def test_a_file_written_reads_back_and_is_listed_with_its_size(
        self, folder: Folder
    ) -> None:
        await folder.write(("a.md",), b"hello")
        assert await folder.read(("a.md",), PLENTY) == b"hello"
        found = await folder.stat(("a.md",))
        assert found is not None
        assert (found.name, found.kind, found.size) == ("a.md", Kind.FILE, 5)
        itself = await folder.stat(())
        assert itself is not None
        assert (itself.name, itself.kind, itself.size) == ("", Kind.FOLDER, None)
        assert names(await folder.list(())) == [("a.md", Kind.FILE)]

    @folder_test
    async def test_a_write_makes_the_folders_on_the_way(self, folder: Folder) -> None:
        await folder.write(("x", "y", "z.md"), b"deep")
        assert names(await folder.list(())) == [("x", Kind.FOLDER)]
        assert names(await folder.list(("x",))) == [("y", Kind.FOLDER)]
        assert names(await folder.list(("x", "y"))) == [("z.md", Kind.FILE)]
        found = await folder.stat(("x",))
        assert found is not None
        assert (found.kind, found.size) == (Kind.FOLDER, None)

    @folder_test
    async def test_a_write_replaces_the_file_whole(self, folder: Folder) -> None:
        await folder.write(("a.md",), b"first version")
        await folder.write(("a.md",), b"2nd")
        assert await folder.read(("a.md",), PLENTY) == b"2nd"
        found = await folder.stat(("a.md",))
        assert found is not None
        assert found.size == 3

    @folder_test
    async def test_a_write_refuses_a_folder_and_a_path_under_a_file(self, folder: Folder) -> None:
        await folder.write(("x", "a.md"), b"a")
        with pytest.raises(IsAFolderError):
            await folder.write(("x",), b"over a folder")
        with pytest.raises(NotAFolderError, match="'x/a.md'"):
            await folder.write(("x", "a.md", "under.md"), b"under a file")
        with pytest.raises(NotAFolderError, match="'x/a.md'"):
            await folder.write(("x", "a.md", "deeper", "under.md"), b"under a file")
        assert await folder.read(("x", "a.md"), PLENTY) == b"a"

    @folder_test
    async def test_a_read_refuses_what_is_not_a_file_within_the_size(self, folder: Folder) -> None:
        await folder.write(("x", "a.md"), b"twelve bytes")
        with pytest.raises(NotFoundError, match="'nope.md'"):
            await folder.read(("nope.md",), PLENTY)
        with pytest.raises(IsAFolderError, match="'x'"):
            await folder.read(("x",), PLENTY)
        with pytest.raises(TooLargeError, match="12 bytes"):
            await folder.read(("x", "a.md"), 11)
        assert await folder.read(("x", "a.md"), 12) == b"twelve bytes"

    @folder_test
    async def test_a_listing_is_by_name_and_refuses_a_file(self, folder: Folder) -> None:
        await folder.write(("b.md",), b"b")
        await folder.write(("a", "inner.md"), b"i")
        await folder.write(("c.md",), b"cc")
        listed = await folder.list(())
        assert names(listed) == [("a", Kind.FOLDER), ("b.md", Kind.FILE), ("c.md", Kind.FILE)]
        assert [e.size for e in listed] == [None, 1, 2]
        with pytest.raises(NotAFolderError, match="'b.md'"):
            await folder.list(("b.md",))
        with pytest.raises(NotFoundError, match="'a/nothing'"):
            await folder.list(("a", "nothing"))

    @folder_test
    async def test_a_move_takes_one_file_to_a_path_with_nothing_at_it(self, folder: Folder) -> None:
        await folder.write(("a.md",), b"a")
        await folder.move(("a.md",), ("x", "b.md"))
        assert await folder.stat(("a.md",)) is None
        assert await folder.read(("x", "b.md"), PLENTY) == b"a"

    @folder_test
    async def test_a_move_refuses_a_folder_a_missing_file_and_a_taken_target(
        self, folder: Folder
    ) -> None:
        await folder.write(("a.md",), b"a")
        await folder.write(("x", "b.md"), b"b")
        with pytest.raises(NotFoundError):
            await folder.move(("nope.md",), ("c.md",))
        with pytest.raises(IsAFolderError, match="'x'"):
            await folder.move(("x",), ("y",))
        with pytest.raises(ExistsError, match="'x/b.md'"):
            await folder.move(("a.md",), ("x", "b.md"))
        with pytest.raises(ExistsError, match="'x'"):
            await folder.move(("a.md",), ("x",))
        with pytest.raises(NotAFolderError, match="'x/b.md'"):
            await folder.move(("a.md",), ("x", "b.md", "c.md"))
        assert await folder.read(("a.md",), PLENTY) == b"a"
        assert await folder.read(("x", "b.md"), PLENTY) == b"b"

    @folder_test
    async def test_a_copy_leaves_the_source_and_refuses_the_same(self, folder: Folder) -> None:
        await folder.write(("a.md",), b"a")
        await folder.copy(("a.md",), ("x", "b.md"))
        assert await folder.read(("a.md",), PLENTY) == b"a"
        assert await folder.read(("x", "b.md"), PLENTY) == b"a"
        with pytest.raises(ExistsError):
            await folder.copy(("a.md",), ("x", "b.md"))
        with pytest.raises(NotFoundError):
            await folder.copy(("nope.md",), ("c.md",))
        with pytest.raises(IsAFolderError):
            await folder.copy(("x",), ("y",))
        with pytest.raises(NotAFolderError):
            await folder.copy(("a.md",), ("a.md", "under.md"))

    @folder_test
    async def test_a_delete_takes_one_file_or_one_empty_folder(self, folder: Folder) -> None:
        await folder.write(("x", "a.md"), b"a")
        await folder.write(("x", "b.md"), b"b")
        with pytest.raises(NotEmptyError, match="'x'"):
            await folder.delete(("x",))
        went = await folder.delete(("x", "a.md"))
        assert (went.name, went.kind, went.size) == ("a.md", Kind.FILE, 1)
        assert names(await folder.list(("x",))) == [("b.md", Kind.FILE)]
        await folder.delete(("x", "b.md"))
        assert names(await folder.list(())) == [("x", Kind.FOLDER)]
        went = await folder.delete(("x",))
        assert (went.name, went.kind) == ("x", Kind.FOLDER)
        assert await folder.list(()) == []
        with pytest.raises(NotFoundError, match="'x'"):
            await folder.delete(("x",))

    @folder_test
    async def test_the_folder_itself_is_neither_read_written_moved_nor_deleted(
        self, folder: Folder
    ) -> None:
        await folder.write(("a.md",), b"a")
        with pytest.raises(PathError):
            await folder.read((), PLENTY)
        with pytest.raises(PathError):
            await folder.write((), b"x")
        with pytest.raises(PathError):
            await folder.move(("a.md",), ())
        with pytest.raises(PathError):
            await folder.move((), ("b.md",))
        with pytest.raises(PathError):
            await folder.copy((), ("b.md",))
        with pytest.raises(PathError):
            await folder.delete(())
        assert names(await folder.list(())) == [("a.md", Kind.FILE)]
