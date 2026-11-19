# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The names ``ROBINAUTS_COMPONENTS`` holds, the components they give and each refusal
(``cli/extra.py``)."""

from __future__ import annotations

import sys
import types
from collections.abc import Mapping

import pytest
from fastapi import APIRouter
from oneberth.identity import RoleTable
from oneberth.web import Component

from robinauts.cli.extra import VARIABLE, extra_components, named
from robinauts.controller.contract.domain import ConfigError

MODULE = "robinauts_test_extra"


def component_of(name: str, environ: Mapping[str, str]) -> Component:
    roles = RoleTable(app=name, permissions=frozenset(), roles=())
    return Component(name=name, roles=roles, router=APIRouter(), title=environ.get("TITLE"))


@pytest.fixture(autouse=True)
def stand_in(monkeypatch: pytest.MonkeyPatch) -> None:
    """A module the server's Python finds, with two applications and two names that give none."""
    module = types.ModuleType(MODULE)
    module.reviews = lambda environ: component_of("reviews", environ)  # type: ignore[attr-defined]
    module.notes = lambda environ: component_of("notes", environ)  # type: ignore[attr-defined]
    module.text = lambda environ: "reviews"  # type: ignore[attr-defined]
    module.value = 3  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, MODULE, module)


@pytest.mark.parametrize(
    ("value", "names"),
    [
        (None, []),
        ("", []),
        (" , ", []),
        ("a:b", ["a:b"]),
        (" a:b , c.d:e ,", ["a:b", "c.d:e"]),
    ],
)
def test_the_variable_holds_names_separated_by_commas(value: str | None, names: list[str]) -> None:
    assert named({} if value is None else {VARIABLE: value}) == names


def test_each_function_is_given_the_environment_and_the_order_is_kept() -> None:
    environ = {VARIABLE: f"{MODULE}:notes, {MODULE}:reviews", "TITLE": "from the environment"}
    components = extra_components(environ)
    assert [c.name for c in components] == ["notes", "reviews"]
    assert {c.title for c in components} == {"from the environment"}


@pytest.mark.parametrize(
    ("name", "refusal"),
    [
        ("reviews", "'reviews' is not <module>:<function>"),
        (f"{MODULE}:", f"'{MODULE}:' is not <module>:<function>"),
        (f"{MODULE}:re-views", f"'{MODULE}:re-views' is not <module>:<function>"),
        ("robinauts_no_such_module:app", "No module named 'robinauts_no_such_module'"),
        (f"{MODULE}:missing", f"{MODULE} has no function missing"),
        (f"{MODULE}:value", f"{MODULE} has no function value"),
        (f"{MODULE}:text", f"{MODULE}:text returned str, not a oneberth Component"),
    ],
)
def test_a_name_that_gives_no_component_is_refused(name: str, refusal: str) -> None:
    with pytest.raises(ConfigError) as refused:
        extra_components({VARIABLE: name})
    assert str(refused.value).startswith(f"{VARIABLE}: ")
    assert refusal in str(refused.value)


def test_every_problem_is_named_at_once() -> None:
    with pytest.raises(ConfigError) as refused:
        extra_components({VARIABLE: f"reviews, {MODULE}:reviews, {MODULE}:missing"})
    assert str(refused.value).splitlines() == [
        f"{VARIABLE}: 'reviews' is not <module>:<function>",
        f"{VARIABLE}: {MODULE}:missing: {MODULE} has no function missing",
    ]
