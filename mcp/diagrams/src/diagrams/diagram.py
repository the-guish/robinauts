# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors
"""The diagrams: rectangles joined by arrows, each laid out again after every change.

They are kept in memory, by id, and are gone when the server stops. An id is 16 random hex
digits, and whoever holds it reaches the diagram. Every change bumps the diagram's revision
and records which items it touched, in the order a view should animate them: node by node in
layout order, each arrow once both of its ends are there.
"""

from __future__ import annotations

import re
import secrets
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from diagrams.layout import Layout, layout

MAX_DIAGRAMS = 200
"""The most diagrams the server holds."""
MAX_NODES = 100
MAX_EDGES = 200
MAX_LABEL = 60
MAX_COORDINATE = 10_000
DIRECTIONS = ("TB", "LR")
_ID = re.compile(r"[A-Za-z0-9_.-]{1,40}")


class DiagramError(ValueError):
    """A change the diagram refuses, with a sentence that says why."""


@dataclass(frozen=True, slots=True)
class Node:
    id: str
    label: str = ""


@dataclass(frozen=True, slots=True)
class Edge:
    source: str
    target: str
    label: str = ""

    @property
    def key(self) -> str:
        return f"{self.source}->{self.target}"


def _label(text: str) -> str:
    text = " ".join(str(text).split())
    if len(text) > MAX_LABEL:
        raise DiagramError(f"a label is at most {MAX_LABEL} characters: {text[:20]!r}...")
    return text


class Diagram:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.id = secrets.token_hex(8)
        self.direction = "TB"
        self.nodes: dict[str, str] = {}  # id -> label, in the order they were added
        self.edges: dict[str, Edge] = {}
        self.pins: dict[str, tuple[float, float]] = {}
        self.revision = 0
        self.changed: list[str] = []
        self.layout: Layout = layout({}, [])

    def draw(self, nodes: Iterable[Node], edges: Iterable[Edge], direction: str) -> None:
        """Replace the whole diagram."""
        if direction not in DIRECTIONS:
            raise DiagramError(f"the direction is one of {', '.join(DIRECTIONS)}")
        with self._lock:
            self._change(lambda: self._replace(list(nodes), list(edges), direction))

    def add(self, nodes: Iterable[Node], edges: Iterable[Edge]) -> None:
        """Add nodes and arrows; a node or an arrow already there takes the new label."""
        with self._lock:
            self._change(lambda: self._upsert(list(nodes), list(edges)))

    def remove(self, ids: Iterable[str]) -> None:
        """Remove nodes, with their arrows, and arrows, named `source->target`."""

        def remove() -> list[str]:
            gone = []
            for key in ids:
                if key in self.nodes:
                    del self.nodes[key]
                    self.pins.pop(key, None)
                    gone += [k for k, e in self.edges.items() if key in (e.source, e.target)]
                elif key not in self.edges:
                    raise DiagramError(f"there is no node or arrow {key!r}")
                gone.append(key)
            for key in gone:
                self.edges.pop(key, None)
            return list(dict.fromkeys(gone))

        with self._lock:
            self._change(remove)

    def pin(self, node: str, x: float | None, y: float | None) -> None:
        """Pin a node's centre at `x`, `y`, or let the layout place it again with no `x`, `y`."""

        def pin() -> list[str]:
            if node not in self.nodes:
                raise DiagramError(f"there is no node {node!r}")
            if x is None or y is None:
                self.pins.pop(node, None)
            elif not (0 <= x <= MAX_COORDINATE and 0 <= y <= MAX_COORDINATE):
                raise DiagramError(f"x and y are between 0 and {MAX_COORDINATE}")
            else:
                self.pins[node] = (round(x), round(y))
            return [node]

        with self._lock:
            self._change(pin)

    def snapshot(self) -> dict[str, object]:
        """The diagram, laid out, as the view draws it."""
        with self._lock:
            boxes = self.layout.boxes
            return {
                "diagram_id": self.id,
                "revision": self.revision,
                "direction": self.direction,
                "width": self.layout.width,
                "height": self.layout.height,
                "nodes": [
                    {
                        "id": node,
                        "lines": list(box.lines),
                        "x": box.x,
                        "y": box.y,
                        "w": box.w,
                        "h": box.h,
                        "pinned": node in self.pins,
                    }
                    for node, box in boxes.items()
                ],
                "edges": [
                    {"key": e.key, "source": e.source, "target": e.target, "label": e.label}
                    for e in self.edges.values()
                ],
                "order": self._build_order(),
                "changed": list(self.changed),
            }

    def describe(self) -> str:
        """The diagram in words, for the model: its id, every node where it is, every arrow."""
        with self._lock:
            named = f"(diagram_id {self.id})"
            if not self.nodes:
                return f"The diagram is empty {named}."
            size = f"{self.layout.width}x{self.layout.height}px"
            lines = [f"Diagram {self.direction}, {size} {named}:"]
            for node, box in self.layout.boxes.items():
                pin = ", pinned" if node in self.pins else ""
                lines.append(f"- {node} {self.nodes[node]!r} at ({box.x:g}, {box.y:g}){pin}")
            lines += [
                f"- {e.key}" + (f" {e.label!r}" if e.label else "") for e in self.edges.values()
            ]
            return "\n".join(lines)

    def _change(self, operation: Callable[[], list[str]]) -> None:
        saved = (self.direction, dict(self.nodes), dict(self.edges), dict(self.pins))
        try:
            touched = operation()
        except DiagramError:
            self.direction, self.nodes, self.edges, self.pins = saved
            raise
        self.layout = layout(
            self.nodes,
            [(e.source, e.target) for e in self.edges.values()],
            self.direction,
            self.pins,
        )
        self.revision += 1
        wanted = set(touched)
        self.changed = [key for key in self._build_order() if key in wanted]
        self.changed += [key for key in touched if key not in self.changed]  # what went

    def _replace(self, nodes: list[Node], edges: list[Edge], direction: str) -> list[str]:
        gone = list(self.nodes) + list(self.edges)
        self.direction, self.nodes, self.edges, self.pins = direction, {}, {}, {}
        return self._upsert(nodes, edges) + [
            key for key in gone if key not in self.nodes and key not in self.edges
        ]

    def _upsert(self, nodes: list[Node], edges: list[Edge]) -> list[str]:
        touched = []
        for node in nodes:
            if not _ID.fullmatch(node.id):
                raise DiagramError(
                    f"a node id is 1 to 40 letters, digits, '_', '.' or '-': {node.id!r}"
                )
            self.nodes[node.id] = _label(node.label) or node.id
            touched.append(node.id)
        for edge in edges:
            for end in (edge.source, edge.target):
                if end not in self.nodes:
                    raise DiagramError(f"the arrow {edge.key} names no node {end!r}")
            if edge.source == edge.target:
                raise DiagramError(f"an arrow joins two nodes: {edge.key}")
            self.edges[edge.key] = Edge(edge.source, edge.target, _label(edge.label))
            touched.append(edge.key)
        if len(self.nodes) > MAX_NODES or len(self.edges) > MAX_EDGES:
            raise DiagramError(f"a diagram holds at most {MAX_NODES} nodes and {MAX_EDGES} arrows")
        return touched

    def _build_order(self) -> list[str]:
        """Every node in layout order, each arrow right after the later of its two ends."""
        seen: set[str] = set()
        order = []
        for node in self.layout.order:
            seen.add(node)
            order.append(node)
            order += [
                e.key
                for e in self.edges.values()
                if node in (e.source, e.target) and {e.source, e.target} <= seen
            ]
        return order


class Diagrams:
    """The diagrams the server holds, by id, in the order they were created."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._held: dict[str, Diagram] = {}

    def keep(self, diagram: Diagram) -> None:
        """Hold a new diagram. A full store first drops the diagram created first."""
        with self._lock:
            while len(self._held) >= MAX_DIAGRAMS:
                del self._held[next(iter(self._held))]
            self._held[diagram.id] = diagram

    def get(self, diagram_id: str) -> Diagram:
        with self._lock:
            if diagram_id not in self._held:
                raise DiagramError(f"there is no diagram {diagram_id!r}")
            return self._held[diagram_id]
