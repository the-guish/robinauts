# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors
"""The MCP server: five tools over the list, and the view a host shows beside them.

`show_todos` names the view in `_meta.ui.resourceUri`, as the MCP Apps extension
(io.modelcontextprotocol/ui) has it, so a host that shows apps can open it. Every tool may be
called by the model and by the view (`_meta.ui.visibility`). The server speaks streamable HTTP
at `/mcp`, statelessly and with plain JSON answers, and asks for no credential: run it on the
loopback address unless something in front of it does the asking.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable, Sequence
from importlib.resources import files
from pathlib import Path
from typing import Annotated

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import BaseModel

from to_do_list.store import Item, StoreError, TodoStore, default_path

VIEW_URI = "ui://to-do-list/app.html"
VIEW_MIME_TYPE = "text/html;profile=mcp-app"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


class TodoItem(BaseModel):
    id: str
    text: str
    done: bool


class TodoList(BaseModel):
    items: list[TodoItem]


def view_html() -> str:
    """The view: one HTML document, its style and script inline, nothing fetched."""
    return files("to_do_list").joinpath("app.html").read_text(encoding="utf-8")


def _ui(**extra: object) -> dict[str, object]:
    return {"ui": {"visibility": ["model", "app"], **extra}}


def _result(items: list[Item]) -> CallToolResult:
    """The list twice: as text the model reads, and as structured content the view renders."""
    if items:
        done = sum(item.done for item in items)
        lines = [f"{len(items)} to-do(s), {done} done:"]
        lines += [f"- [{'x' if item.done else ' '}] {item.text} (id {item.id})" for item in items]
        text = "\n".join(lines)
    else:
        text = "The to-do list is empty."
    structured = TodoList(items=[TodoItem(id=i.id, text=i.text, done=i.done) for i in items])
    return CallToolResult(
        content=[TextContent(type="text", text=text)],
        structuredContent=structured.model_dump(mode="json"),
    )


def _change(operation: Callable[[], list[Item]]) -> CallToolResult:
    try:
        return _result(operation())
    except StoreError as error:
        raise ToolError(str(error)) from None


def build_server(store: TodoStore, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> FastMCP:
    html = view_html()
    server = FastMCP(
        "to-do-list",
        instructions="A to-do list. Show it, add to it, tick items off, remove them.",
        host=host,
        port=port,
        stateless_http=True,
        json_response=True,
        log_level="WARNING",
    )

    @server.resource(
        VIEW_URI,
        name="to-do-list-app",
        title="To-do list",
        description="The to-do list as an interactive view.",
        mime_type=VIEW_MIME_TYPE,
    )
    def app() -> str:
        return html

    @server.tool(
        title="To-do list",
        description="Show the to-do list: every item with its id, its text and whether it is done.",
        annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False),
        meta=_ui(resourceUri=VIEW_URI),
    )
    def show_todos() -> Annotated[CallToolResult, TodoList]:
        return _change(store.items)

    @server.tool(
        title="Add a to-do",
        description="Add a to-do to the end of the list. Returns the whole list.",
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False),
        meta=_ui(),
    )
    def add_todo(text: str) -> Annotated[CallToolResult, TodoList]:
        return _change(lambda: store.add(text))

    @server.tool(
        title="Edit a to-do",
        description="Replace the text of the to-do with this id. Returns the whole list.",
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False
        ),
        meta=_ui(),
    )
    def edit_todo(id: str, text: str) -> Annotated[CallToolResult, TodoList]:
        return _change(lambda: store.edit(id, text))

    @server.tool(
        title="Tick or untick a to-do",
        description="Mark the to-do with this id done, or not done if it was. Returns the list.",
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False),
        meta=_ui(),
    )
    def toggle_todo(id: str) -> Annotated[CallToolResult, TodoList]:
        return _change(lambda: store.toggle(id))

    @server.tool(
        title="Remove a to-do",
        description="Remove the to-do with this id from the list. Returns the list.",
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False),
        meta=_ui(),
    )
    def remove_todo(id: str) -> Annotated[CallToolResult, TodoList]:
        return _change(lambda: store.remove(id))

    return server


def _arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="robinauts-to-do-list",
        description="A to-do list as an MCP server, over streamable HTTP at /mcp.",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("TO_DO_LIST_HOST", DEFAULT_HOST),
        help=f"the address to listen on (TO_DO_LIST_HOST; default {DEFAULT_HOST})",
    )
    parser.add_argument(
        "--port",
        type=int,
        # A string default goes through `type` too, so a bad variable is refused like a bad flag.
        default=os.environ.get("TO_DO_LIST_PORT", str(DEFAULT_PORT)),
        help=f"the port to listen on (TO_DO_LIST_PORT; default {DEFAULT_PORT})",
    )
    parser.add_argument(
        "--file",
        type=Path,
        default=os.environ.get("TO_DO_LIST_FILE") or None,
        help="the JSON file the list is kept in (TO_DO_LIST_FILE; default: "
        "$XDG_DATA_HOME/robinauts-to-do-list/todos.json, or ~/.local/share/... without it)",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _arguments(argv)
    store = TodoStore(arguments.file or default_path())
    try:
        store.check()
    except (StoreError, OSError) as error:
        print(f"robinauts-to-do-list: {error}", file=sys.stderr)
        return 2
    server = build_server(store, arguments.host, arguments.port)
    print(
        f"to-do list at http://{arguments.host}:{arguments.port}/mcp, kept in {store.path}",
        file=sys.stderr,
        flush=True,
    )
    server.run("streamable-http")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
