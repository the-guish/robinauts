# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors
"""The diagram: its changes, what each one touched, in order, and what it refuses."""

import pytest

from diagrams.diagram import Diagram, DiagramError, Edge, Node


@pytest.fixture
def diagram():
    drawn = Diagram()
    drawn.draw([Node("a", "API"), Node("b"), Node("c")], [Edge("a", "b"), Edge("a", "c")], "TB")
    return drawn


def test_a_drawing_is_built_node_by_node_each_arrow_after_its_ends(diagram):
    snapshot = diagram.snapshot()
    assert snapshot["revision"] == 1
    assert snapshot["order"] == ["a", "b", "a->b", "c", "a->c"]
    assert snapshot["changed"] == snapshot["order"]
    assert [node["lines"] for node in snapshot["nodes"]] == [["API"], ["b"], ["c"]]


def test_an_addition_touches_what_it_added_and_nothing_else(diagram):
    diagram.add([Node("d")], [Edge("c", "d", "uses")])
    snapshot = diagram.snapshot()
    assert snapshot["revision"] == 2
    assert snapshot["changed"] == ["d", "c->d"]
    assert snapshot["edges"][-1] == {"key": "c->d", "source": "c", "target": "d", "label": "uses"}


def test_removing_a_node_removes_its_arrows(diagram):
    diagram.remove(["a"])
    snapshot = diagram.snapshot()
    assert [n["id"] for n in snapshot["nodes"]] == ["b", "c"]
    assert snapshot["edges"] == []
    assert set(snapshot["changed"]) == {"a", "a->b", "a->c"}


def test_a_pin_holds_until_it_is_taken_off(diagram):
    diagram.pin("b", 400, 300)
    node = diagram.snapshot()["nodes"][1]
    assert (node["x"], node["y"], node["pinned"]) == (400, 300, True)
    assert "- b 'b' at (400, 300), pinned" in diagram.describe()
    diagram.pin("b", None, None)
    assert diagram.snapshot()["nodes"][1]["pinned"] is False


@pytest.mark.parametrize(
    ("change", "why"),
    [
        (lambda d: d.add([Node("x y")], []), "a node id is"),
        (lambda d: d.add([Node("x", "y" * 61)], []), "at most 60 characters"),
        (lambda d: d.add([], [Edge("a", "nope")]), "names no node 'nope'"),
        (lambda d: d.add([], [Edge("a", "a")]), "joins two nodes"),
        (lambda d: d.add([Node(str(i)) for i in range(98)], []), "at most 100 nodes"),
        (lambda d: d.remove(["nope"]), "no node or arrow 'nope'"),
        (lambda d: d.pin("nope", 1, 1), "no node 'nope'"),
        (lambda d: d.pin("a", -1, 1), "between 0 and"),
        (lambda d: d.draw([], [], "RL"), "the direction is one of"),
    ],
)
def test_a_refused_change_says_why_and_changes_nothing(diagram, change, why):
    before = diagram.snapshot()
    with pytest.raises(DiagramError, match=why):
        change(diagram)
    assert diagram.snapshot() == before
