# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors
"""The MCP server: tools that draw and arrange diagrams, and the view a host shows.

A model draws a diagram with `draw_diagram`, which answers the diagram's id, and shows it with
`show_diagram`. `show_diagram` names the view in `_meta.ui.resourceUri` (MCP Apps,
io.modelcontextprotocol/ui) and is the one tool a view may call: the view reads its diagram
again with it every 2 seconds. The other tools are the model's. Every tool answers with the
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
from typing import Annotated, Literal

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import BaseModel, Field

from diagrams.diagram import Diagram, DiagramError, Diagrams, Edge, Node

VIEW_URI = "ui://diagrams/view.html"
VIEW_MIME_TYPE = "text/html;profile=mcp-app"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8766
MODEL_ONLY = {"ui": {"visibility": ["model"]}}
DiagramId = Annotated[str, Field(description="The diagram's id, as draw_diagram answered it.")]


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
    diagrams = Diagrams()
    server = FastMCP(
        "diagrams",
        instructions="Diagrams of rectangles and arrows. Draw one with draw_diagram, which "
        "answers the diagram's id. Call show_diagram with that id to show the diagram to the "
        "user, drawn step by step, in a side panel. Later changes to a diagram on show appear "
        "there by themselves. The server lays a diagram out; pin a node only when the user asks "
        "to move it.",
        host=host,
        port=port,
        stateless_http=True,
        json_response=True,
        log_level="WARNING",
    )

    def answer(
        diagram_id: str | None, change: Callable[[Diagram], None] = lambda diagram: None
    ) -> CallToolResult:
        """The diagram of that id, after `change`. No id makes a new diagram."""
        try:
            diagram = Diagram() if diagram_id is None else diagrams.get(diagram_id)
            change(diagram)
        except DiagramError as error:
            raise ToolError(str(error)) from None
        if diagram_id is None:
            diagrams.keep(diagram)  # a new diagram is kept once its drawing is accepted
        return CallToolResult(
            content=[TextContent(type="text", text=diagram.describe())],
            structuredContent=diagram.snapshot(),
        )

    def to_nodes(specs: list[NodeSpec]) -> list[Node]:
        return [Node(spec.id, spec.label) for spec in specs]

    def to_edges(specs: list[ArrowSpec]) -> list[Edge]:
        return [Edge(spec.source, spec.target, spec.label) for spec in specs]

    @server.resource(VIEW_URI, name="diagram", title="Diagrams", mime_type=VIEW_MIME_TYPE)
    def view() -> str:
        return html

    @server.tool(
        title="Diagram",
        description="Show a diagram to the user in the side panel. It stays on show: later "
        "changes to it appear there by themselves. Answers every node with its position, and "
        "every arrow.",
        annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False),
        meta={"ui": {"resourceUri": VIEW_URI, "visibility": ["model", "app"]}},
    )
    def show_diagram(diagram_id: DiagramId) -> CallToolResult:
        return answer(diagram_id)

    @server.tool(
        title="Draw a diagram",
        description="Draw a diagram of these nodes (rectangles) and arrows, laid out top to "
        "bottom (TB) or left to right (LR). With no diagram_id it makes a new diagram and "
        "answers its id: call show_diagram with that id to show it to the user. With a "
        "diagram_id it replaces that diagram.",
        annotations=ToolAnnotations(destructiveHint=True, openWorldHint=False),
        meta=MODEL_ONLY,
    )
    def draw_diagram(
        nodes: list[NodeSpec],
        arrows: list[ArrowSpec] | None = None,
        direction: Literal["TB", "LR"] = "TB",
        diagram_id: Annotated[
            str | None, Field(description="The diagram to replace. Leave it out for a new one.")
        ] = None,
    ) -> CallToolResult:
        return answer(
            diagram_id or None,  # some models send "" for an argument they leave out
            lambda diagram: diagram.draw(to_nodes(nodes), to_edges(arrows or []), direction),
        )

    @server.tool(
        title="Add to a diagram",
        description="Add nodes and arrows to a diagram. A node or arrow already there takes "
        "the new label. An arrow may name nodes added in the same call.",
        annotations=ToolAnnotations(destructiveHint=False, openWorldHint=False),
        meta=MODEL_ONLY,
    )
    def add_to_diagram(
        diagram_id: DiagramId,
        nodes: list[NodeSpec] | None = None,
        arrows: list[ArrowSpec] | None = None,
    ) -> CallToolResult:
        return answer(
            diagram_id,
            lambda diagram: diagram.add(to_nodes(nodes or []), to_edges(arrows or [])),
        )

    @server.tool(
        title="Remove from a diagram",
        description="Remove nodes, with their arrows, and arrows, named 'source->target'.",
        annotations=ToolAnnotations(destructiveHint=True, openWorldHint=False),
        meta=MODEL_ONLY,
    )
    def remove_from_diagram(diagram_id: DiagramId, ids: list[str]) -> CallToolResult:
        return answer(diagram_id, lambda diagram: diagram.remove(ids))

    @server.tool(
        title="Pin a node",
        description="Move a node's centre to x, y (pixels from the top-left corner) and keep it "
        "there through later changes. Nodes it covers move aside. Use it when the user asks.",
        annotations=ToolAnnotations(destructiveHint=False, openWorldHint=False),
        meta=MODEL_ONLY,
    )
    def pin_node(diagram_id: DiagramId, id: str, x: float, y: float) -> CallToolResult:
        return answer(diagram_id, lambda diagram: diagram.pin(id, x, y))

    @server.tool(
        title="Unpin a node",
        description="Let the layout place a pinned node again.",
        annotations=ToolAnnotations(destructiveHint=False, openWorldHint=False),
        meta=MODEL_ONLY,
    )
    def unpin_node(diagram_id: DiagramId, id: str) -> CallToolResult:
        return answer(diagram_id, lambda diagram: diagram.pin(id, None, None))

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
