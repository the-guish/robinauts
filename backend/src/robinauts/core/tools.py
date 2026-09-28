# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Naming the tools a run is shown, from what its servers list.

A server lists its tools under its own names (``domain.ListedTool``); the model
is shown each under the **full** name ``<prefix>__<name>``, the server's prefix
and the tool's own, so that a call is routed to its server from the name alone
and two servers offering ``search`` never collide (``docs/specs/agents.md``,
"Tools"). What this module decides is the rules of that naming, pure and
testable with a list:

- a tool whose full name is not one the vendors take -- too long, or spelt
  with something outside ``[A-Za-z0-9_-]`` -- is **left out** of the run's list,
  named, so that the application can say so in the log; a server that lists a
  name the vendors would refuse must not fail every turn of every agent that
  names it;
- so is a tool whose input schema is not a JSON Schema **object** at the top
  (``type: object``), which is what both vendors require of a tool's
  parameters and what a top-level ``anyOf`` is not;
- so is a tool a server lists twice, the second time;
- the list is **sorted by full name**, so that two engines and two runs send
  byte-identical lists, which is what a cached prefix wants.

A full name is split back into its server's prefix and the tool's own name at
the first separator, which a prefix may not hold (``domain.is_tool_prefix``).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from robinauts.domain import (
    TOOL_NAME_SEPARATOR,
    ListedTool,
    ToolDefinition,
    ToolServerConfig,
    is_tool_name,
)


@dataclass(frozen=True, slots=True)
class LeftOut:
    """One listed tool the run is not shown, and why, for the log line naming it."""

    server_id: str
    name: str
    """The tool's name as the server listed it."""
    reason: str


def named_tools(
    server: ToolServerConfig, listed: Iterable[ListedTool]
) -> tuple[tuple[ToolDefinition, ...], tuple[LeftOut, ...]]:
    """The tools that server lists as the model is shown them, and those left out.

    In the server's own order; ``tools_for_run`` is what sorts across servers.
    """
    kept: list[ToolDefinition] = []
    left_out: list[LeftOut] = []
    seen: set[str] = set()
    for tool in listed:
        full = f"{server.prefix}{TOOL_NAME_SEPARATOR}{tool.name}"
        if tool.name in seen:
            left_out.append(LeftOut(server.id, tool.name, "the server listed it twice"))
        elif not is_tool_name(full):
            left_out.append(
                LeftOut(
                    server.id,
                    tool.name,
                    f"its full name {full!r} is not one the vendors take: letters, digits,"
                    f" '_' and '-', at most 64 of them",
                )
            )
        elif tool.input_schema.get("type") != "object":
            left_out.append(
                LeftOut(
                    server.id,
                    tool.name,
                    "its input schema is not an object at the top, which a tool's parameters"
                    " have to be",
                )
            )
        else:
            kept.append(
                ToolDefinition(
                    name=full,
                    description=tool.description,
                    input_schema=tool.input_schema,
                    annotations=tool.annotations,
                )
            )
        seen.add(tool.name)
    return tuple(kept), tuple(left_out)


def tools_for_run(
    servers: Sequence[ToolServerConfig], listed: Mapping[str, Sequence[ListedTool]]
) -> tuple[tuple[ToolDefinition, ...], tuple[LeftOut, ...]]:
    """The one list a run is handed: every server's tools, named, sorted by full name.

    ``listed`` is what each server answered ``tools/list`` with, by server id;
    a server the agent names that is not in it listed nothing. The prefixes are
    distinct by configuration (``domain.ModelsConfig``), so two full names are
    equal only when one server listed one name twice, and that one is left
    out here.
    """
    kept: list[ToolDefinition] = []
    left_out: list[LeftOut] = []
    for server in servers:
        named, missing = named_tools(server, listed.get(server.id, ()))
        kept.extend(named)
        left_out.extend(missing)
    return tuple(sorted(kept, key=lambda tool: tool.name)), tuple(left_out)


def split_tool_name(full: str) -> tuple[str, str] | None:
    """The server's prefix and the tool's own name a full name is made of.

    ``None`` for a name with no separator in it, which is a name the platform
    did not give and no server can be found for: a model that wrote one is
    told so in an error result, not failed (``docs/specs/agents.md``).
    """
    prefix, separator, name = full.partition(TOOL_NAME_SEPARATOR)
    if not separator or not prefix or not name:
        return None
    return prefix, name
