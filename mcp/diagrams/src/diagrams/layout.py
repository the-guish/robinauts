# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors
"""A small layered layout, after dagre's steps and far simpler than dagre.

1. Ranks: back edges found by a depth-first walk are set aside, and each node goes one rank
   below the lowest of its parents (the longest path from a source).
2. Order: within each rank, nodes are sorted by the mean position of their neighbours in the
   ranks above, then below, a few times over (barycentres), to cut crossings.
3. Positions: each node is pulled towards its neighbours' centres, then pushed apart to keep
   the gap, rank by rank. Ranks are stacked along the direction.
4. Pins: a pinned node sits where it is pinned, and a node it overlaps is pushed aside.

Every position is a node's centre, in pixels, from the drawing's top-left corner.
"""

from __future__ import annotations

import textwrap
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

CHAR_WIDTH = 7.2
"""A generous width of one character of the view's 13px sans-serif font."""
LINE_HEIGHT = 17
PADDING = 14
MIN_WIDTH = 96
LINE_CHARS = 26
"""The most characters on one line of a label; a longer label wraps."""
GAP = 32
"""Between two nodes of one rank."""
RANK_GAP = 56
"""Between two ranks."""
MARGIN = 24
SWEEPS = 4


@dataclass(frozen=True, slots=True)
class Box:
    x: float
    y: float
    w: float
    h: float
    lines: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Layout:
    boxes: dict[str, Box]
    order: list[str]
    """The nodes rank by rank, and in each rank from first to last."""
    width: float
    height: float


def label_box(label: str) -> tuple[float, float, tuple[str, ...]]:
    """A label's lines, and the width and height of the rectangle that holds them."""
    lines = tuple(textwrap.wrap(label, LINE_CHARS, break_long_words=True)) or ("",)
    width = max(MIN_WIDTH, round(max(map(len, lines)) * CHAR_WIDTH + 2 * PADDING))
    return width, len(lines) * LINE_HEIGHT + 2 * PADDING - 4, lines


def _ranks(ids: Sequence[str], edges: Sequence[tuple[str, str]]) -> dict[str, int]:
    children: dict[str, list[str]] = {node: [] for node in ids}
    for source, target in edges:
        children[source].append(target)
    state: dict[str, int] = {}  # 1: on the walk's path, 2: done
    forward: list[tuple[str, str]] = []

    def walk(node: str) -> None:
        state[node] = 1
        for child in children[node]:
            if state.get(child) != 1:  # an edge back up the path would make a cycle
                forward.append((node, child))
                if child not in state:
                    walk(child)
        state[node] = 2

    for node in ids:
        if node not in state:
            walk(node)
    rank = dict.fromkeys(ids, 0)
    for _ in ids:  # longest path: at most one pass per node
        changed = False
        for source, target in forward:
            if rank[target] < rank[source] + 1:
                rank[target], changed = rank[source] + 1, True
        if not changed:
            break
    return rank


def _mean(values: list[float], fallback: float) -> float:
    return sum(values) / len(values) if values else fallback


def layout(
    labels: Mapping[str, str],
    edges: Sequence[tuple[str, str]],
    direction: str = "TB",
    pins: Mapping[str, tuple[float, float]] | None = None,
) -> Layout:
    """Lay out the nodes `labels` names, in their order, joined by `edges`, top to bottom
    (`TB`) or left to right (`LR`)."""
    ids = list(labels)
    sizes = {node: label_box(labels[node]) for node in ids}
    across = 0 if direction == "TB" else 1  # which of width, height lies along a rank
    rank = _ranks(ids, edges)
    neighbours: dict[str, list[str]] = {node: [] for node in ids}
    for source, target in edges:
        neighbours[source].append(target)
        neighbours[target].append(source)
    rows: list[list[str]] = [[] for _ in range(max(rank.values(), default=-1) + 1)]
    for node in ids:
        rows[rank[node]].append(node)

    def near(node: str, downward: bool) -> list[str]:
        """The node's neighbours in the ranks above it, or below it."""
        r = rank[node]
        return [n for n in neighbours[node] if (rank[n] < r if downward else rank[n] > r)]

    # Order: barycentres, down then up.
    place = {node: float(index) for row in rows for index, node in enumerate(row)}
    for sweep in range(SWEEPS):
        downward = sweep % 2 == 0
        for row in rows if downward else reversed(rows):
            for node in row:
                place[node] = _mean([place[n] for n in near(node, downward)], place[node])
            row.sort(key=lambda node: place[node])
            for index, node in enumerate(row):
                place[node] = float(index)

    # Positions across the ranks: towards the neighbours, then apart.
    centre: dict[str, float] = {}
    for row in rows:
        edge = 0.0
        for node in row:
            size = sizes[node][across]
            centre[node], edge = edge + size / 2, edge + size + GAP
    for sweep in range(SWEEPS):
        downward = sweep % 2 == 0
        for row in rows if downward else reversed(rows):
            wanted = [
                _mean([centre[n] for n in near(node, downward)], centre[node]) for node in row
            ]
            edge = float("-inf")
            for node, want in zip(row, wanted, strict=True):
                half = sizes[node][across] / 2
                centre[node] = max(want, edge + half)
                edge = centre[node] + half + GAP
            shift = _mean([w - centre[n] for n, w in zip(row, wanted, strict=True)], 0.0)
            for node in row:
                centre[node] += shift
    low = min((centre[n] - sizes[n][across] / 2 for n in ids), default=0.0)

    # Ranks stacked along the direction, then the pins.
    boxes: dict[str, Box] = {}
    along = MARGIN
    for row in rows:
        depth = max(sizes[node][1 - across] for node in row)
        for node in row:
            width, height, lines = sizes[node]
            cross = centre[node] - low + MARGIN
            x, y = (cross, along + depth / 2) if across == 0 else (along + depth / 2, cross)
            boxes[node] = Box(round(x), round(y), width, height, lines)
        along += depth + RANK_GAP
    pinned: dict[str, Box] = {}
    for node, (x, y) in (pins or {}).items():
        if node in boxes:
            pinned[node] = boxes[node] = Box(x, y, *sizes[node])
    for node in ids:
        if node not in pinned:
            boxes[node] = _clear_of(boxes[node], pinned.values(), across)

    order = [node for row in rows for node in row]
    width = max((b.x + b.w / 2 for b in boxes.values()), default=0) + MARGIN
    height = max((b.y + b.h / 2 for b in boxes.values()), default=0) + MARGIN
    return Layout(boxes, order, round(width), round(height))


def _clear_of(box: Box, pinned: Iterable[Box], across: int) -> Box:
    """`box`, pushed along its rank past every pinned box it overlaps."""
    for _ in range(64):
        hit = next((p for p in pinned if _overlap(box, p)), None)
        if hit is None:
            return box
        if across == 0:
            box = Box(round(hit.x + (hit.w + box.w) / 2 + GAP), box.y, box.w, box.h, box.lines)
        else:
            box = Box(box.x, round(hit.y + (hit.h + box.h) / 2 + GAP), box.w, box.h, box.lines)
    return box


def _overlap(a: Box, b: Box) -> bool:
    return abs(a.x - b.x) * 2 < a.w + b.w + GAP and abs(a.y - b.y) * 2 < a.h + b.h + GAP
