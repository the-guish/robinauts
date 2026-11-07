# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""What every function of the tool does with what the model sent: check it, in words the model
can act on, and answer as JSON. The frameworks hand the arguments over as the model wrote
them, so each is checked here, and a path is one ``relative_path`` accepts or the call is
refused before the folder is asked anything.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Collection, Mapping
from typing import Any

from robinauts.agent_engines.contract.domain import FunctionTool, FunctionToolError
from robinauts.filesystems.contract.domain import (
    FileSystemError,
    PathError,
    RelativePath,
    relative_path,
)

MAX_FILE_BYTES = 1_048_576
"""Bytes: the largest file these tools read, write or edit. Notes, not archives."""

Run = Callable[[Mapping[str, Any]], Awaitable[dict[str, Any]]]
"""A function of the tool: the checked arguments in, what the model is told out."""


def function(name: str, description: str, schema: Mapping[str, Any], run: Run) -> FunctionTool:
    """The function as the engines take it: its answer as JSON, and a refusal of the folder's
    as a failed call the model reads."""

    async def call(arguments: Mapping[str, Any]) -> str:
        try:
            return json.dumps(await run(arguments), ensure_ascii=False)
        except FileSystemError as refused:
            raise FunctionToolError(str(refused)) from refused

    return FunctionTool(name, description, schema, call)


def known(arguments: Mapping[str, Any], names: Collection[str], required: Collection[str]) -> None:
    """Refuse an argument the function does not take, and the lack of one it needs."""
    if unknown := sorted(set(arguments) - set(names)):
        raise FunctionToolError(f"Unknown argument(s): {', '.join(unknown)}.")
    if missing := sorted(set(required) - set(arguments)):
        raise FunctionToolError(f"Missing argument(s): {', '.join(missing)}.")


def path(arguments: Mapping[str, Any], name: str = "path", *, folder: bool = False) -> RelativePath:
    """The path argument ``name``, checked. ``folder``: ``""`` or none at all is the folder
    itself; else the path names a file or a folder inside it."""
    given = arguments.get(name, "" if folder else None)
    if not isinstance(given, str):
        raise FunctionToolError(f"`{name}` must be a string: a path inside your folder.")
    if not folder and not given:
        raise FunctionToolError(f"`{name}` must name a file inside your folder, such as 'a.md'.")
    try:
        return relative_path(given)
    except PathError as refused:
        raise FunctionToolError(f"`{name}`: {refused}") from refused


def text(arguments: Mapping[str, Any], name: str, *, empty: bool = True) -> str:
    """The string argument ``name``, of at most ``MAX_FILE_BYTES`` of UTF-8."""
    given = arguments.get(name)
    if not isinstance(given, str):
        raise FunctionToolError(f"`{name}` must be a string.")
    if not empty and given == "":
        raise FunctionToolError(f"`{name}` must not be empty.")
    if len(given.encode("utf-8")) > MAX_FILE_BYTES:
        raise FunctionToolError(f"`{name}` is longer than {MAX_FILE_BYTES} bytes of UTF-8.")
    return given


def integer(
    arguments: Mapping[str, Any], name: str, *, minimum: int, maximum: int | None = None
) -> int | None:
    """The whole-number argument ``name`` within its bounds, or ``None`` when it was left out."""
    if name not in arguments:
        return None
    given = arguments[name]
    bounds = f"at least {minimum}" if maximum is None else f"from {minimum} to {maximum}"
    if isinstance(given, bool) or not isinstance(given, int):
        raise FunctionToolError(f"`{name}` must be a whole number, {bounds}.")
    if given < minimum or (maximum is not None and given > maximum):
        raise FunctionToolError(f"`{name}` must be {bounds}, not {given}.")
    return given


def required_integer(
    arguments: Mapping[str, Any], name: str, *, minimum: int, maximum: int | None = None
) -> int:
    """``integer``, for an argument the function cannot do without."""
    value = integer(arguments, name, minimum=minimum, maximum=maximum)
    if value is None:
        raise FunctionToolError(f"Missing argument(s): {name}.")
    return value
