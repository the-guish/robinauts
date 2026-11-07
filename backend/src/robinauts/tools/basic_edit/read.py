# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""``read_file`` and ``list_folder``: what the model reads of its folder.

A read is bounded, by lines and by characters, and says so when it was cut and where to go on
from. A listing is bounded in depth and in entries, and names no pattern: the model asks for a
folder and sees what is in it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from robinauts.agent_engines.contract.domain import FunctionTool, FunctionToolError
from robinauts.filesystems.contract.domain import Entry, Kind, RelativePath, joined
from robinauts.filesystems.contract.ports import Folder
from robinauts.tools.basic_edit import arguments, text

MAX_LINES_PER_READ = 1000
"""Lines: the most one read answers."""

MAX_CHARS_PER_READ = 50_000
"""Characters: the most one read answers, counted over whole lines."""

MAX_DEPTH = 5
"""How deep a listing may go: 1 is the folder named alone."""

MAX_ENTRIES = 500
"""Entries: the most one listing answers."""

READ_FILE = "read_file"

READ_FILE_DESCRIPTION = (
    "Read a text file in your folder: the whole file, the lines from `from_line` to `to_line`"
    " (1-based, both included), or the last `tail` lines. Lines end at a newline. The answer"
    " says how many lines the file has and which were returned; a long read is cut at"
    f" {MAX_LINES_PER_READ} lines or {MAX_CHARS_PER_READ} characters, says so with"
    " `truncated`, and `next_line` is where to go on from. `content` is exactly the text of"
    " those lines."
)

READ_FILE_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": "The file, relative to your folder, such as 'notes/today.md'.",
        },
        "from_line": {
            "type": "integer",
            "description": "The first line to return, 1 if left out.",
            "minimum": 1,
        },
        "to_line": {
            "type": "integer",
            "description": "The last line to return, the file's last if left out.",
            "minimum": 1,
        },
        "tail": {
            "type": "integer",
            "description": "Return the last `tail` lines instead; not with `from_line`/`to_line`.",
            "minimum": 1,
        },
    },
    "required": ["path"],
    "additionalProperties": False,
}

LIST_FOLDER = "list_folder"

LIST_FOLDER_DESCRIPTION = (
    "List what a folder of yours holds: files with their size in bytes and folders, by name,"
    " each with its path relative to your folder. `path` left out or empty is your folder"
    f" itself. `depth` 1 lists that folder alone; up to {MAX_DEPTH} goes into its folders too,"
    f" each folder's entries right after it. At most {MAX_ENTRIES} entries are returned, and"
    " `truncated` says when there were more. There is no pattern or search: name a folder."
)

LIST_FOLDER_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": "The folder, relative to your folder; empty or left out for yours.",
        },
        "depth": {
            "type": "integer",
            "description": f"How deep to go, 1 to {MAX_DEPTH}; 1 if left out.",
            "minimum": 1,
            "maximum": MAX_DEPTH,
        },
    },
    "additionalProperties": False,
}


def read_file_tool(folder: Folder) -> FunctionTool:
    async def run(given: Mapping[str, Any]) -> dict[str, Any]:
        arguments.known(given, ("path", "from_line", "to_line", "tail"), ("path",))
        path = arguments.path(given)
        from_line = arguments.integer(given, "from_line", minimum=1)
        to_line = arguments.integer(given, "to_line", minimum=1)
        tail = arguments.integer(given, "tail", minimum=1)
        if tail is not None and (from_line is not None or to_line is not None):
            raise FunctionToolError("`tail` cannot be given with `from_line` or `to_line`.")
        if from_line is not None and to_line is not None and to_line < from_line:
            raise FunctionToolError("`to_line` must not be before `from_line`.")
        data = await folder.read(path, arguments.MAX_FILE_BYTES)
        lines, ends_with_newline = text.split(text.decode(data, joined(path)))
        return read(joined(path), lines, ends_with_newline, from_line, to_line, tail)

    return arguments.function(READ_FILE, READ_FILE_DESCRIPTION, READ_FILE_SCHEMA, run)


def read(
    path: str,
    lines: list[str],
    ends_with_newline: bool,
    from_line: int | None,
    to_line: int | None,
    tail: int | None,
) -> dict[str, Any]:
    """The answer of ``read_file`` for a file already read and split."""
    total = len(lines)
    if total == 0:
        return {
            "path": path,
            "total_lines": 0,
            "from_line": 0,
            "to_line": 0,
            "truncated": False,
            "content": "",
        }
    if tail is not None:
        start, end = max(1, total - tail + 1), total
    else:
        start = from_line or 1
        end = total if to_line is None else min(to_line, total)
        if start > total:
            raise FunctionToolError(f"{path!r} has {total} lines; `from_line` {start} is past it.")
    chosen: list[str] = []
    characters = 0
    for line in lines[start - 1 : end]:
        full = len(chosen) >= MAX_LINES_PER_READ or characters + len(line) + 1 > MAX_CHARS_PER_READ
        if chosen and full:
            break
        chosen.append(line)
        characters += len(line) + 1
    last = start + len(chosen) - 1
    truncated = last < end
    content = text.join(chosen, ends_with_newline or last < total)
    answer: dict[str, Any] = {
        "path": path,
        "total_lines": total,
        "from_line": start,
        "to_line": last,
        "truncated": truncated,
        "content": content,
    }
    if truncated:
        answer["next_line"] = last + 1
    return answer


def list_folder_tool(folder: Folder) -> FunctionTool:
    async def run(given: Mapping[str, Any]) -> dict[str, Any]:
        arguments.known(given, ("path", "depth"), ())
        path = arguments.path(given, folder=True)
        depth = arguments.integer(given, "depth", minimum=1, maximum=MAX_DEPTH) or 1
        entries: list[dict[str, Any]] = []
        truncated = await _walk(folder, path, depth, entries)
        return {"path": joined(path), "depth": depth, "entries": entries, "truncated": truncated}

    return arguments.function(LIST_FOLDER, LIST_FOLDER_DESCRIPTION, LIST_FOLDER_SCHEMA, run)


async def _walk(folder: Folder, path: RelativePath, depth: int, out: list[dict[str, Any]]) -> bool:
    """The entries under ``path`` to ``depth``, each folder's right after it, into ``out``:
    true when ``MAX_ENTRIES`` cut it short."""
    for entry in await folder.list(path):
        if len(out) >= MAX_ENTRIES:
            return True
        child = (*path, entry.name)
        out.append(_record(child, entry))
        if entry.kind is Kind.FOLDER and depth > 1 and await _walk(folder, child, depth - 1, out):
            return True
    return False


def _record(path: RelativePath, entry: Entry) -> dict[str, Any]:
    record: dict[str, Any] = {"path": joined(path), "kind": entry.kind.value}
    if entry.kind is Kind.FILE:
        record["size"] = entry.size
    if entry.modified_at is not None:
        record["modified_at"] = entry.modified_at.isoformat()
    return record
