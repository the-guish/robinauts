# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""``move_file``, ``copy_file`` and ``delete_file``: one file at a time, by its path.

A move or a copy goes to a path with nothing at it, never over a file; a delete takes one file,
or one folder with nothing left in it. No pattern names several.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from robinauts.agent_engines.contract.domain import FunctionTool
from robinauts.filesystems.contract.domain import joined
from robinauts.filesystems.contract.ports import Folder
from robinauts.tools.basic_edit import arguments

MOVE_FILE = "move_file"

MOVE_FILE_DESCRIPTION = (
    "Move or rename one file in your folder, from `path` to `to`. Refused when something is"
    " already at `to`; folders on the way are made. Files only: a folder is not moved."
)

COPY_FILE = "copy_file"

COPY_FILE_DESCRIPTION = (
    "Copy one file in your folder, from `path` to `to`. Refused when something is already at"
    " `to`; folders on the way are made. Files only: a folder is not copied."
)

TWO_PATHS_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "The file, relative to your folder."},
        "to": {"type": "string", "description": "Its new path, relative to your folder."},
    },
    "required": ["path", "to"],
    "additionalProperties": False,
}

DELETE_FILE = "delete_file"

DELETE_FILE_DESCRIPTION = (
    "Delete one file in your folder, or one empty folder. A folder with anything in it is"
    " refused: delete what it holds first, one by one."
)

DELETE_FILE_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": "The file or the empty folder, relative to your folder.",
        }
    },
    "required": ["path"],
    "additionalProperties": False,
}


def move_file_tool(folder: Folder) -> FunctionTool:
    async def run(given: Mapping[str, Any]) -> dict[str, Any]:
        arguments.known(given, ("path", "to"), ("path", "to"))
        source, target = arguments.path(given), arguments.path(given, "to")
        await folder.move(source, target)
        return {"path": joined(source), "to": joined(target)}

    return arguments.function(MOVE_FILE, MOVE_FILE_DESCRIPTION, TWO_PATHS_SCHEMA, run)


def copy_file_tool(folder: Folder) -> FunctionTool:
    async def run(given: Mapping[str, Any]) -> dict[str, Any]:
        arguments.known(given, ("path", "to"), ("path", "to"))
        source, target = arguments.path(given), arguments.path(given, "to")
        await folder.copy(source, target)
        return {"path": joined(source), "to": joined(target)}

    return arguments.function(COPY_FILE, COPY_FILE_DESCRIPTION, TWO_PATHS_SCHEMA, run)


def delete_file_tool(folder: Folder) -> FunctionTool:
    async def run(given: Mapping[str, Any]) -> dict[str, Any]:
        arguments.known(given, ("path",), ("path",))
        path = arguments.path(given)
        went = await folder.delete(path)
        return {"path": joined(path), "deleted": went.kind.value}

    return arguments.function(DELETE_FILE, DELETE_FILE_DESCRIPTION, DELETE_FILE_SCHEMA, run)
