# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors
"""The layout: ranks follow the arrows, nothing overlaps, pins hold."""

import pytest

from diagrams.layout import GAP, layout

CHAIN = (["a", "b", "c"], [("a", "b"), ("b", "c")])
TREE = (["root", "x", "y", "z"], [("root", "x"), ("root", "y"), ("root", "z")])
CYCLE = (["a", "b", "c"], [("a", "b"), ("b", "c"), ("c", "a")])
DIAMOND = (["s", "l", "r", "t"], [("s", "l"), ("s", "r"), ("l", "t"), ("r", "t")])
LONG = (["a", "b", "c", "d"], [("a", "b"), ("b", "c"), ("a", "d"), ("c", "d")])
LONELY = (["only", "and", "alone"], [])


def _run(graph, direction="TB", pins=None):
    ids, edges = graph
    return layout({node: f"Node {node}" for node in ids}, edges, direction, pins)


def _apart(a, b):
    return abs(a.x - b.x) * 2 >= a.w + b.w + GAP - 1 or abs(a.y - b.y) * 2 >= a.h + b.h - 1


@pytest.mark.parametrize("graph", [CHAIN, TREE, CYCLE, DIAMOND, LONG, LONELY])
@pytest.mark.parametrize("direction", ["TB", "LR"])
def test_no_two_nodes_overlap_and_all_lie_inside_the_drawing(graph, direction):
    result = _run(graph, direction)
    boxes = list(result.boxes.values())
    assert sorted(result.boxes) == sorted(graph[0]) == sorted(result.order)
    for i, a in enumerate(boxes):
        assert a.x - a.w / 2 >= 0
        assert a.y - a.h / 2 >= 0
        assert a.x + a.w / 2 <= result.width
        assert a.y + a.h / 2 <= result.height
        for b in boxes[i + 1 :]:
            assert _apart(a, b)


@pytest.mark.parametrize(
    ("graph", "direction", "axis"),
    [(CHAIN, "TB", "y"), (CHAIN, "LR", "x"), (DIAMOND, "TB", "y"), (LONG, "LR", "x")],
)
def test_every_arrow_points_down_or_right(graph, direction, axis):
    boxes = _run(graph, direction).boxes
    for source, target in graph[1]:
        assert getattr(boxes[target], axis) > getattr(boxes[source], axis)


def test_a_cycle_keeps_all_but_one_arrow_pointing_forward():
    boxes = _run(CYCLE).boxes
    assert boxes["a"].y < boxes["b"].y < boxes["c"].y


def test_a_parent_sits_above_the_middle_of_its_children():
    boxes = _run(TREE).boxes
    assert boxes["root"].x == pytest.approx((boxes["x"].x + boxes["z"].x) / 2, abs=1)


def test_a_long_label_wraps_and_widens_its_box():
    result = layout({"n": "word " * 11, "m": "m"}, [])
    assert len(result.boxes["n"].lines) == 3
    assert result.boxes["n"].w > result.boxes["m"].w
    assert result.boxes["n"].h > result.boxes["m"].h


def test_a_pinned_node_sits_where_it_is_pinned_and_the_others_make_room():
    free = _run(TREE).boxes
    pinned = _run(TREE, pins={"x": (free["y"].x, free["y"].y)}).boxes
    assert (pinned["x"].x, pinned["x"].y) == (free["y"].x, free["y"].y)
    assert _apart(pinned["x"], pinned["y"])
    assert _apart(pinned["x"], pinned["z"])


def test_the_same_diagram_is_laid_out_the_same_way():
    assert _run(DIAMOND) == _run(DIAMOND)
