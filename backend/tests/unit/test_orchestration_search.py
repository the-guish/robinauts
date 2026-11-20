# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The lists of values searched as a person types (``orchestrator/core/search.py``): the
branches a repository's settings offer, the names a branch may have, and a search's ranking."""

from __future__ import annotations

import pytest

from robinauts.orchestrator.contract.domain import Option, OptionPage
from robinauts.orchestrator.core.search import is_branch_name, matches, named, offered, search

HEADS = ("feature/a", "feature/b", "main", "next", "release/1", "release/1/hotfix")


@pytest.mark.parametrize(
    ("entries", "offers"),
    [
        (("main",), ("main",)),
        (("next", "main"), ("next", "main")),
        (("main", "gone"), ("main",)),
        (("*",), HEADS),
        (
            ("main", "*"),
            ("main", "feature/a", "feature/b", "next", "release/1", "release/1/hotfix"),
        ),
        (("release/*",), ("release/1", "release/1/hotfix")),
        (("feature/?",), ("feature/a", "feature/b")),
        (("feature/[a]",), ("feature/a",)),
        (("Main",), ()),
    ],
)
def test_a_repository_offers_the_branches_its_entries_match(
    entries: tuple[str, ...], offers: tuple[str, ...]
) -> None:
    assert offered(HEADS, entries) == offers


@pytest.mark.parametrize(
    ("branch", "entries", "matched"),
    [
        ("main", ("main",), True),
        ("mainline", ("main",), False),
        ("release/2", ("main", "release/*"), True),
        ("feature/x/y", ("feature/*",), True),
    ],
)
def test_a_branch_matches_an_entry_that_names_it_or_a_pattern_it_fits(
    branch: str, entries: tuple[str, ...], matched: bool
) -> None:
    assert matches(branch, entries) is matched


def test_only_the_entries_that_name_one_branch_are_named() -> None:
    assert named(("main", "release/*", "next", "v?", "[ab]")) == ("main", "next")


@pytest.mark.parametrize(
    ("name", "allowed"),
    [
        ("main", True),
        ("feature/add-retry", True),
        ("v1.2.3", True),
        ("", False),
        ("-delete", False),
        ("/main", False),
        ("main/", False),
        ("main.", False),
        ("main.lock", False),
        ("a/.hidden", False),
        ("a/b.lock/c", False),
        ("a..b", False),
        ("a//b", False),
        ("a b", False),
        ("a\tb", False),
        ("a~1", False),
        ("a^", False),
        ("a:b", False),
        ("a?", False),
        ("a*", False),
        ("a[b", False),
        ("a\\b", False),
        ("a@{1}", False),
        ("@", False),
        ("x" * 255, True),
        ("x" * 256, False),
    ],
)
def test_a_start_names_only_what_git_takes_as_a_branch(name: str, allowed: bool) -> None:
    assert is_branch_name(name) is allowed


OPTIONS = tuple(
    Option(value, label)
    for value, label in [
        ("a1", "release/main-old"),
        ("a2", "main"),
        ("a3", "maintenance"),
        ("a4", "Feature/MAIN"),
        ("a5", "docs"),
    ]
)


@pytest.mark.parametrize(
    ("query", "limit", "values", "more"),
    [
        ("", 10, ["a1", "a2", "a3", "a4", "a5"], False),
        ("", 2, ["a1", "a2"], True),
        ("main", 10, ["a2", "a3", "a1", "a4"], False),
        ("MAIN", 10, ["a2", "a3", "a1", "a4"], False),
        ("  main ", 10, ["a2", "a3", "a1", "a4"], False),
        ("main", 1, ["a2"], True),
        ("a5", 10, ["a5"], False),
        ("feature/", 10, ["a4"], False),
        ("nothing", 10, [], False),
    ],
)
def test_a_search_ranks_the_exact_then_the_start_then_the_rest(
    query: str, limit: int, values: list[str], more: bool
) -> None:
    page = search(OPTIONS, query, limit)
    assert page == OptionPage(
        items=tuple(o for v in values for o in OPTIONS if o.value == v), more=more
    )
