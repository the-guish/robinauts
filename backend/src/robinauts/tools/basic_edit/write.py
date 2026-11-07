# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""``write_file``, ``replace_in_file`` and ``replace_lines``: how the model changes a file.

Each writes one file whole, however it was asked to change it, so a file is never half
edited. ``replace_in_file`` replaces a literal text, every time it occurs, and never a pattern.

The size a file may have is checked **before** a result is built, wherever the result could be
larger than what went in: a replacement's size is what the counts say it will be, and a text
that would pass the bound is refused without the memory for it ever being asked for. What the
model sends is bounded on its own, so the rest is bounded by what it reads plus what it sent.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from robinauts.agent_engines.contract.domain import FunctionTool, FunctionToolError
from robinauts.filesystems.contract.domain import RelativePath, joined
from robinauts.filesystems.contract.ports import Folder
from robinauts.tools.basic_edit import arguments, text

WRITE_FILE = "write_file"

WRITE_FILE_DESCRIPTION = (
    "Create a text file in your folder, or replace one whole, with `content`. Folders on the"
    f" way are made. UTF-8; at most {arguments.MAX_FILE_BYTES} bytes."
)

WRITE_FILE_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": "The file, relative to your folder, such as 'notes/today.md'.",
        },
        "content": {"type": "string", "description": "The whole new content of the file."},
    },
    "required": ["path", "content"],
    "additionalProperties": False,
}

REPLACE_IN_FILE = "replace_in_file"

REPLACE_IN_FILE_DESCRIPTION = (
    "Replace a text in a file of your folder: every occurrence of `old`, exactly as written,"
    " becomes `new`. Not a pattern: the characters themselves, case included. Refused when"
    " `old` is nowhere in the file, so read the file first and copy the text exactly. The"
    " answer says how many occurrences were replaced."
)

REPLACE_IN_FILE_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "The file, relative to your folder."},
        "old": {"type": "string", "description": "The text to find, exactly; not empty."},
        "new": {"type": "string", "description": "What takes its place; may be empty."},
    },
    "required": ["path", "old", "new"],
    "additionalProperties": False,
}

REPLACE_LINES = "replace_lines"

REPLACE_LINES_DESCRIPTION = (
    "Replace the lines `from_line` to `to_line` of a file in your folder (1-based, both"
    " included, as `read_file` numbers them) with `content`, which may hold any number of"
    " lines or be empty to delete them. To insert without replacing, give `to_line` ="
    " `from_line` - 1: the content goes in before `from_line`; `from_line` one past the last"
    " line appends. Read the lines first: the numbers must be the file's as it is now."
)

REPLACE_LINES_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "The file, relative to your folder."},
        "from_line": {
            "type": "integer",
            "description": "The first line to replace, from 1 to one past the last line.",
            "minimum": 1,
        },
        "to_line": {
            "type": "integer",
            "description": "The last line to replace; `from_line` - 1 replaces none and inserts.",
            "minimum": 0,
        },
        "content": {"type": "string", "description": "The lines that take their place."},
    },
    "required": ["path", "from_line", "to_line", "content"],
    "additionalProperties": False,
}


def _within_limit(path: RelativePath, size: int) -> None:
    """Refuse a result of ``size`` bytes of UTF-8, before it exists when it can be known first."""
    if size > arguments.MAX_FILE_BYTES:
        raise FunctionToolError(
            f"That would make {joined(path)!r} {size} bytes, and these tools work on files of at"
            f" most {arguments.MAX_FILE_BYTES} bytes."
        )


async def _write_text(folder: Folder, path: RelativePath, content: str) -> int:
    """The text written, as UTF-8, within ``MAX_FILE_BYTES``: how many bytes."""
    data = content.encode("utf-8")
    _within_limit(path, len(data))
    await folder.write(path, data)
    return len(data)


async def _read_text(folder: Folder, path: RelativePath) -> str:
    return text.decode(await folder.read(path, arguments.MAX_FILE_BYTES), joined(path))


def write_file_tool(folder: Folder) -> FunctionTool:
    async def run(given: Mapping[str, Any]) -> dict[str, Any]:
        arguments.known(given, ("path", "content"), ("path", "content"))
        path = arguments.path(given)
        content = arguments.text(given, "content")
        created = await folder.stat(path) is None
        size = await _write_text(folder, path, content)
        return {"path": joined(path), "bytes": size, "created": created}

    return arguments.function(WRITE_FILE, WRITE_FILE_DESCRIPTION, WRITE_FILE_SCHEMA, run)


def replace_in_file_tool(folder: Folder) -> FunctionTool:
    async def run(given: Mapping[str, Any]) -> dict[str, Any]:
        arguments.known(given, ("path", "old", "new"), ("path", "old", "new"))
        path = arguments.path(given)
        old = arguments.text(given, "old", empty=False)
        new = arguments.text(given, "new")
        data = await folder.read(path, arguments.MAX_FILE_BYTES)
        content = text.decode(data, joined(path))
        count = content.count(old)
        if count == 0:
            raise FunctionToolError(
                f"`old` is nowhere in {joined(path)!r}. Read the file and copy the text exactly."
            )
        # Every occurrence of `old` is exactly its own UTF-8 in the file, so the result's size
        # is known before the result is: refused here, or it could be `count` times `new`.
        grows_by = len(new.encode("utf-8")) - len(old.encode("utf-8"))
        _within_limit(path, len(data) + count * grows_by)
        await _write_text(folder, path, content.replace(old, new))
        return {"path": joined(path), "replacements": count}

    return arguments.function(
        REPLACE_IN_FILE, REPLACE_IN_FILE_DESCRIPTION, REPLACE_IN_FILE_SCHEMA, run
    )


def replace_lines_tool(folder: Folder) -> FunctionTool:
    async def run(given: Mapping[str, Any]) -> dict[str, Any]:
        names = ("path", "from_line", "to_line", "content")
        arguments.known(given, names, names)
        path = arguments.path(given)
        from_line = arguments.required_integer(given, "from_line", minimum=1)
        to_line = arguments.required_integer(given, "to_line", minimum=0)
        content = arguments.text(given, "content")
        lines, ends_with_newline = text.split(await _read_text(folder, path))
        total = len(lines)
        if from_line > total + 1:
            raise FunctionToolError(
                f"{joined(path)!r} has {total} lines; `from_line` can be at most {total + 1}."
            )
        if to_line < from_line - 1:
            raise FunctionToolError("`to_line` must be at least `from_line` - 1.")
        if to_line > total:
            raise FunctionToolError(
                f"{joined(path)!r} has {total} lines; `to_line` can be at most {total}."
            )
        new_lines, _ = text.split(content)
        lines[from_line - 1 : to_line] = new_lines
        await _write_text(folder, path, text.join(lines, ends_with_newline))
        return {
            "path": joined(path),
            "from_line": from_line,
            "to_line": to_line,
            "lines_removed": to_line - from_line + 1,
            "lines_added": len(new_lines),
            "total_lines": len(lines),
        }

    return arguments.function(REPLACE_LINES, REPLACE_LINES_DESCRIPTION, REPLACE_LINES_SCHEMA, run)
