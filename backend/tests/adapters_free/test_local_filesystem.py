# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The local file system keeps the folder's contract, and adds what a disk alone can need: a
link that leads out of the folder is refused, a write leaves nothing half done behind, and the
folders are where ``docs/specs/files.md`` says."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from robinauts.filesystems.contract.domain import Kind, OutsideFolderError, RootUnusableError
from robinauts.filesystems.contract.ports import Folder
from robinauts.filesystems.local import LocalFileSystem, LocalFolder
from util.aio import asyncio_test
from util.contracts.folder import FolderContract

pytestmark = pytest.mark.io

OWNER = "3f2a0c7e-8e1b-4b7a-9f44-1f2c3d4e5f60"


@pytest.mark.io
class TestLocalFolder(FolderContract):
    @pytest.fixture(autouse=True)
    def _directory(self, tmp_path: Path) -> None:
        self.directory = tmp_path / "folder"

    async def new_folder(self) -> Folder:
        return LocalFolder(self.directory)


@asyncio_test
async def test_a_folder_is_root_owner_agent_and_is_made_on_the_first_write(
    tmp_path: Path,
) -> None:
    root = tmp_path / "files"
    files = LocalFileSystem(root)
    await files.setup()
    assert root.is_dir()
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    folder = files.folder(OWNER, "agent-1")
    assert not (root / OWNER).exists()
    await folder.write(("notes", "a.md"), b"a")
    assert (root / OWNER / "agent-1" / "notes" / "a.md").read_bytes() == b"a"
    # Another owner, another agent: other folders, which see nothing of this one.
    assert await files.folder("another-owner", "agent-1").list(()) == []
    assert await files.folder(OWNER, "agent-2").list(()) == []


@asyncio_test
async def test_setup_is_safe_to_repeat_and_leaves_an_existing_root_as_it_is(
    tmp_path: Path,
) -> None:
    root = tmp_path / "files"
    root.mkdir(mode=0o750)
    files = LocalFileSystem(root)
    await files.setup()
    await files.setup()
    assert stat.S_IMODE(root.stat().st_mode) == 0o750


@asyncio_test
async def test_setup_refuses_a_root_that_cannot_be_used(tmp_path: Path) -> None:
    a_file = tmp_path / "a-file"
    a_file.write_text("x")
    with pytest.raises(RootUnusableError, match="cannot be made"):
        await LocalFileSystem(a_file).setup()
    with pytest.raises(RootUnusableError, match="cannot be made"):
        await LocalFileSystem(a_file / "under").setup()


def test_a_name_that_cannot_name_a_folder_is_refused(tmp_path: Path) -> None:
    files = LocalFileSystem(tmp_path)
    for owner, agent in ((OWNER, "a/b"), ("..", "agent"), ("", "agent"), (OWNER, ".")):
        with pytest.raises(ValueError, match="not the name of a folder"):
            files.folder(owner, agent)


@asyncio_test
async def test_a_link_that_leads_out_of_the_folder_is_refused_everywhere(tmp_path: Path) -> None:
    outside = tmp_path / "outside.md"
    outside.write_text("secret")
    directory = tmp_path / "folder"
    directory.mkdir()
    (directory / "link.md").symlink_to(outside)
    (directory / "dir-link").symlink_to(tmp_path)
    folder = LocalFolder(directory)
    with pytest.raises(OutsideFolderError, match="'link.md'"):
        await folder.read(("link.md",), 1000)
    with pytest.raises(OutsideFolderError):
        await folder.write(("link.md",), b"over it")
    with pytest.raises(OutsideFolderError):
        await folder.stat(("dir-link",))
    with pytest.raises(OutsideFolderError):
        await folder.list(("dir-link",))
    with pytest.raises(OutsideFolderError, match="'dir-link/outside.md'"):
        await folder.read(("dir-link", "outside.md"), 1000)
    with pytest.raises(OutsideFolderError):
        await folder.write(("dir-link", "new.md"), b"through it")
    with pytest.raises(OutsideFolderError):
        await folder.delete(("link.md",))
    with pytest.raises(OutsideFolderError):
        await folder.move(("link.md",), ("moved.md",))
    with pytest.raises(OutsideFolderError):
        await folder.copy(("link.md",), ("copied.md",))
    assert outside.read_text() == "secret"
    assert not (tmp_path / "new.md").exists()


@asyncio_test
async def test_a_link_inside_the_folder_is_the_link_it_is(tmp_path: Path) -> None:
    directory = tmp_path / "folder"
    directory.mkdir()
    (directory / "real.md").write_text("real")
    (directory / "link.md").symlink_to(directory / "real.md")
    (directory / "dangling.md").symlink_to(directory / "gone.md")
    folder = LocalFolder(directory)
    assert await folder.read(("link.md",), 1000) == b"real"
    assert [e.name for e in await folder.list(())] == ["link.md", "real.md"]
    went = await folder.delete(("link.md",))
    assert went.kind is Kind.FILE
    assert (directory / "real.md").read_text() == "real"
    assert not (directory / "link.md").is_symlink()


@asyncio_test
async def test_a_write_leaves_only_the_file_behind(tmp_path: Path) -> None:
    folder = LocalFolder(tmp_path / "folder")
    await folder.write(("notes", "a.md"), b"first")
    await folder.write(("notes", "a.md"), b"second")
    assert os.listdir(tmp_path / "folder" / "notes") == ["a.md"]
    assert (tmp_path / "folder" / "notes" / "a.md").read_bytes() == b"second"
