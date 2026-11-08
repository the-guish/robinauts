# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors
"""The MCP server: tools that draw and arrange one diagram, and the view a host shows.

`show_diagram` names the view in `_meta.ui.resourceUri` (MCP Apps, io.modelcontextprotocol/ui)
and is the one tool a view may call; the others are the model's. Every tool answers with the
diagram twice: in words for the model, and laid out, as structured content, for the view.
The server speaks streamable HTTP at `/mcp`, statelessly, and asks for no credential: run it
on the loopback address unless something in front of it does the asking.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable, Sequence
from importlib.resources import files
from typing import Literal

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import BaseModel, Field

from diagrams.diagram import Diagram, DiagramError, Edge, Node

VIEW_URI = "ui://diagrams/view.html"
VIEW_MIME_TYPE = "text/html;profile=mcp-app"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8766
MODEL_ONLY = {"ui": {"visibility": ["model"]}}


class NodeSpec(BaseModel):
    id: str = Field(description="Letters, digits, '_', '.' or '-'; what arrows name.")
    label: str = Field("", description="The text in the rectangle; the id when empty.")


class ArrowSpec(BaseModel):
    source: str = Field(description="The id of the node the arrow starts from.")
    target: str = Field(description="The id of the node the arrow points to.")
    label: str = ""


def view_html() -> str:
    """The view: one HTML document, its style and script inline, nothing fetched."""
    return files("diagrams").joinpath("view.html").read_text(encoding="utf-8")


def build_server(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> FastMCP:
    html = view_html()
    diagram = Diagram()
    server = FastMCP(
        "diagrams",
        instructions="A diagram of rectangles and arrows that the user sees drawn, animated, in "
        "a side panel. The server lays it out; pin a node only when the user asks to move it.",
        host=host,
        port=port,
        stateless_http=True,
        json_response=True,
        log_level="WARNING",
    )

    def answer(change: Callable[[], None] = lambda: None) -> CallToolResult:
        try:
            change()
        except DiagramError as error:
            raise ToolError(str(error)) from None
        return CallToolResult(
            content=[TextContent(type="text", text=diagram.describe())],
            structuredContent=diagram.snapshot(),
        )

    def to_nodes(specs: list[NodeSpec]) -> list[Node]:
        return [Node(spec.id, spec.label) for spec in specs]

    def to_edges(specs: list[ArrowSpec]) -> list[Edge]:
        return [Edge(spec.source, spec.target, spec.label) for spec in specs]

    @server.resource(VIEW_URI, name="diagram", title="Diagram", mime_type=VIEW_MIME_TYPE)
    def view() -> str:
        return html

    @server.tool(
        title="Diagram",
        description="Show the diagram: every node with its position, every arrow.",
        annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False),
        meta={"ui": {"resourceUri": VIEW_URI, "visibility": ["model", "app"]}},
    )
    def show_diagram() -> CallToolResult:
        return answer()

    @server.tool(
        title="Draw a diagram",
        description="Replace the diagram with these nodes (rectangles) and arrows, laid out "
        "top to bottom (TB) or left to right (LR). The user sees it drawn step by step.",
        annotations=ToolAnnotations(destructiveHint=True, openWorldHint=False),
        meta=MODEL_ONLY,
    )
    def draw_diagram(
        nodes: list[NodeSpec],
        arrows: list[ArrowSpec] | None = None,
        direction: Literal["TB", "LR"] = "TB",
    ) -> CallToolResult:
        return answer(lambda: diagram.draw(to_nodes(nodes), to_edges(arrows or []), direction))

    @server.tool(
        title="Add to the diagram",
        description="Add nodes and arrows to the diagram. A node or arrow already there takes "
        "the new label. An arrow may name nodes added in the same call.",
        annotations=ToolAnnotations(destructiveHint=False, openWorldHint=False),
        meta=MODEL_ONLY,
    )
    def add_to_diagram(
        nodes: list[NodeSpec] | None = None,
        arrows: list[ArrowSpec] | None = None,
    ) -> CallToolResult:
        return answer(lambda: diagram.add(to_nodes(nodes or []), to_edges(arrows or [])))

    @server.tool(
        title="Remove from the diagram",
        description="Remove nodes, with their arrows, and arrows, named 'source->target'.",
        annotations=ToolAnnotations(destructiveHint=True, openWorldHint=False),
        meta=MODEL_ONLY,
    )
    def remove_from_diagram(ids: list[str]) -> CallToolResult:
        return answer(lambda: diagram.remove(ids))

    @server.tool(
        title="Pin a node",
        description="Move a node's centre to x, y (pixels from the top-left corner) and keep it "
        "there through later changes. Nodes it covers move aside. Use it when the user asks.",
        annotations=ToolAnnotations(destructiveHint=False, openWorldHint=False),
        meta=MODEL_ONLY,
    )
    def pin_node(id: str, x: float, y: float) -> CallToolResult:
        return answer(lambda: diagram.pin(id, x, y))

    @server.tool(
        title="Unpin a node",
        description="Let the layout place a pinned node again.",
        annotations=ToolAnnotations(destructiveHint=False, openWorldHint=False),
        meta=MODEL_ONLY,
    )
    def unpin_node(id: str) -> CallToolResult:
        return answer(lambda: diagram.pin(id, None, None))

    return server


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="robinauts-diagrams",
        description="Diagrams an agent draws, as an MCP server, over streamable HTTP at /mcp.",
    )
    parser.add_argument("--host", default=os.environ.get("DIAGRAMS_HOST", DEFAULT_HOST))
    parser.add_argument(
        "--port", type=int, default=os.environ.get("DIAGRAMS_PORT", str(DEFAULT_PORT))
    )
    arguments = parser.parse_args(argv)
    server = build_server(arguments.host, arguments.port)
    print(f"diagrams at http://{arguments.host}:{arguments.port}/mcp", file=sys.stderr, flush=True)
    server.run("streamable-http")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
