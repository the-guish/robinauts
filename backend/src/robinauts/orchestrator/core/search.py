# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The lists of values searched as a person types: which branches of a repository its entry in
the flows' settings offers, what a branch's name may be, and the values that match a search.
Pure: the branches and the values are handed in."""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from fnmatch import fnmatchcase

from robinauts.orchestrator.contract.domain import Option, OptionPage

PATTERN_CHARACTERS = frozenset("*?[")
"""What makes an entry of a repository's ``branches`` a pattern rather than a branch's name."""

BRANCH_NAME_LIMIT = 255
"""The longest branch name a start may name."""

_REFUSED_IN_A_NAME = re.compile(r"[\x00-\x20\x7f~^:?*\[\\]|\.\.|@\{|//")


def is_pattern(entry: str) -> bool:
    """Whether an entry of ``branches`` matches several branches, as a shell pattern does."""
    return not PATTERN_CHARACTERS.isdisjoint(entry)


def named(entries: Iterable[str]) -> tuple[str, ...]:
    """The entries of ``branches`` that name one branch each, in their order."""
    return tuple(entry for entry in entries if not is_pattern(entry))


def matches(branch: str, entries: Iterable[str]) -> bool:
    """Whether an entry of ``branches`` is ``branch``, or a pattern ``branch`` matches. A ``*``
    matches across a ``/`` too."""
    return any(fnmatchcase(branch, entry) for entry in entries)


def offered(heads: Iterable[str], entries: Sequence[str]) -> tuple[str, ...]:
    """The branches of ``heads`` the entries match: those an entry names first, in the entries'
    order, then the rest in the order of their names."""
    present = set(heads)
    first = [entry for entry in named(entries) if entry in present]
    rest = sorted(b for b in present - set(first) if matches(b, entries))
    return (*dict.fromkeys(first), *rest)


def is_branch_name(name: str) -> bool:
    """Whether ``name`` may be a branch's, as git's ``check-ref-format --branch`` has it, and
    within the limit: what a start may send to a repository's server."""
    if not name or len(name) > BRANCH_NAME_LIMIT or _REFUSED_IN_A_NAME.search(name):
        return False
    if name.startswith(("-", "/")) or name.endswith(("/", ".", ".lock")) or name == "@":
        return False
    return not any(part.startswith(".") or part.endswith(".lock") for part in name.split("/"))


def search(options: Sequence[Option], query: str, limit: int) -> OptionPage:
    """The options that match ``query``, at most ``limit``: those whose value or label is it
    first, then those that start with it, then those that hold it, each in the order given,
    case aside. An empty query matches every option."""
    wanted = query.strip().casefold()
    exact, starting, holding = [], [], []
    for option in options:
        texts = (option.value.casefold(), option.label.casefold())
        if wanted in texts:
            exact.append(option)
        elif any(text.startswith(wanted) for text in texts):
            starting.append(option)
        elif any(wanted in text for text in texts):
            holding.append(option)
    found = [*exact, *starting, *holding]
    return OptionPage(items=tuple(found[:limit]), more=len(found) > limit)
