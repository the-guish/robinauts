# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""What crosses the file system port: the paths inside a folder, the entries it holds, and the
refusals. The meaning is ``docs/specs/files.md``.

The one rule that bounds every file operation is here, in ``relative_path``: a path a tool is
handed names something inside its folder, or it is refused before any adapter sees it. An
adapter is handed the segments and nothing else, so what a path may say is decided once, in
one function, whatever the storage behind it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

RelativePath = tuple[str, ...]
"""A path inside a folder, as its segments: ``()`` is the folder itself. Made by
``relative_path`` alone, so every segment is one ``plain`` name."""

MAX_PATH_CHARS = 1024
"""Characters: the longest path a tool takes."""

MAX_SEGMENT_BYTES = 255
"""Bytes of UTF-8: the longest name of a file or a folder, which is what the common file
systems allow."""

SEPARATOR = "/"
"""The one separator a path uses, on every kind of storage."""


class FileSystemError(Exception):
    """A refusal of the file system, in words the model can act on."""


class PathError(FileSystemError):
    """A path that names nothing inside the folder."""


class NotFoundError(FileSystemError):
    def __init__(self, path: str) -> None:
        super().__init__(f"Nothing at {path!r}.")


class ExistsError(FileSystemError):
    def __init__(self, path: str) -> None:
        super().__init__(f"There is already a file or a folder at {path!r}.")


class IsAFolderError(FileSystemError):
    def __init__(self, path: str) -> None:
        super().__init__(f"{path!r} is a folder, not a file.")


class NotAFolderError(FileSystemError):
    def __init__(self, path: str) -> None:
        super().__init__(f"{path!r} is a file, not a folder.")


class NotEmptyError(FileSystemError):
    def __init__(self, path: str) -> None:
        super().__init__(f"The folder {path!r} is not empty: delete what is in it first.")


class TooLargeError(FileSystemError):
    def __init__(self, path: str, size: int, limit: int) -> None:
        super().__init__(
            f"{path!r} is {size} bytes, and these tools work on files of at most {limit} bytes."
        )


class OutsideFolderError(FileSystemError):
    """A path that resolved to something outside the folder, which a link inside it alone can
    cause: the tools cannot make one."""

    def __init__(self, path: str) -> None:
        super().__init__(f"{path!r} leads outside the folder and is refused.")


class RootUnusableError(FileSystemError):
    """The place a file system keeps every folder cannot be used: it cannot be made, is not a
    folder, or cannot be written. Raised by an adapter's ``setup``, for the operator."""


class Kind(StrEnum):
    FILE = "file"
    FOLDER = "folder"


@dataclass(frozen=True, slots=True)
class Entry:
    """What a folder holds, by name: a file, with its size, or a folder."""

    name: str
    kind: Kind
    size: int | None = None
    """Bytes, for a file; ``None`` for a folder."""
    modified_at: datetime | None = None
    """When it was last written, when the storage knows."""


def plain(name: str) -> bool:
    """Whether ``name`` is one name of a file or a folder: not empty, not ``.`` or ``..``,
    no separator and no character that is not printable, and at most ``MAX_SEGMENT_BYTES``
    of UTF-8. Whitespace inside a name is allowed; a name that is whitespace alone is not."""
    if not name or not name.strip() or name in (".", ".."):
        return False
    if SEPARATOR in name or "\\" in name or not name.isprintable():
        return False
    return len(name.encode("utf-8")) <= MAX_SEGMENT_BYTES


def relative_path(text: str) -> RelativePath:
    """The segments of ``text``, a path inside a folder with ``/`` between its names: ``()``
    for the empty string, which is the folder itself. ``PathError`` for anything else: a
    path that starts or ends with ``/``, has an empty name, ``.`` or ``..`` in it, a
    backslash, a character that is not printable, a name too long or a path too long."""
    if not isinstance(text, str):
        raise PathError("A path is a string.")
    if len(text) > MAX_PATH_CHARS:
        raise PathError(f"A path is at most {MAX_PATH_CHARS} characters.")
    if text == "":
        return ()
    if text.startswith(SEPARATOR):
        raise PathError(
            f"{text!r} starts with '/': a path is relative to your folder and names what is"
            " inside it, such as 'notes/today.md'."
        )
    if text.endswith(SEPARATOR):
        raise PathError(f"{text!r} ends with '/': name the file or the folder itself.")
    segments = tuple(text.split(SEPARATOR))
    for segment in segments:
        if segment in (".", ".."):
            raise PathError(f"{text!r} has {segment!r} in it: a path cannot go up or stay put.")
        if not plain(segment):
            raise PathError(
                f"{text!r} has a name that is not allowed: a name is not empty, has no '/' or"
                f" backslash and no control character, and is at most {MAX_SEGMENT_BYTES}"
                " bytes."
            )
    return segments


def joined(path: RelativePath) -> str:
    """The path as a tool shows it: its segments with ``/`` between, ``""`` for the folder."""
    return SEPARATOR.join(path)
