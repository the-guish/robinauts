# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The basic edit tools: the model keeps files in one folder, the person's with this agent.

Eight functions, each on one file or one folder at a time, by its path and nothing else: no
pattern, no search, no walk past the depth asked (``docs/specs/files.md``). Where the folder
is, and that nothing outside it can be named, is not this tool's to decide: it works through
the ``Folder`` the turn's scope hands it, which the worker bound to the owner and the agent, and
every path it takes is one ``relative_path`` accepted.
"""

from __future__ import annotations

from robinauts.agent_engines.contract.domain import FunctionTool
from robinauts.tools.basic_edit.move import copy_file_tool, delete_file_tool, move_file_tool
from robinauts.tools.basic_edit.read import list_folder_tool, read_file_tool
from robinauts.tools.basic_edit.write import (
    replace_in_file_tool,
    replace_lines_tool,
    write_file_tool,
)
from robinauts.tools.contract.ports import ToolProvider, TurnScope

ID = "basic_edit"
"""What an agent's ``tools`` names this tool by."""


class BasicEditTool(ToolProvider):
    __slots__ = ()

    @property
    def id(self) -> str:
        return ID

    def tools_for(self, scope: TurnScope) -> list[FunctionTool]:
        folder = scope.folder
        if folder is None:
            raise RuntimeError(f"the {ID!r} tool was given a turn with no folder")
        return [
            read_file_tool(folder),
            list_folder_tool(folder),
            write_file_tool(folder),
            replace_in_file_tool(folder),
            replace_lines_tool(folder),
            move_file_tool(folder),
            copy_file_tool(folder),
            delete_file_tool(folder),
        ]


def load() -> ToolProvider:
    return BasicEditTool()
