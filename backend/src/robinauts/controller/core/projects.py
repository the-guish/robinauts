# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""A project's name, as a person typed it and as it is kept."""

from __future__ import annotations

from robinauts.controller.contract.domain import InvalidValueError

MAX_PROJECT_NAME_CHARS = 60
"""Counted in code points, as Python counts."""


def project_name(text: str) -> str:
    """The name kept for ``text``: one line, its runs of white space made one space, and its
    ends trimmed. ``InvalidValueError`` for a name left empty or longer than
    ``MAX_PROJECT_NAME_CHARS``."""
    name = " ".join("".join(c if c.isprintable() else " " for c in text).split())
    if not name:
        raise InvalidValueError("a project's name needs something in it")
    if len(name) > MAX_PROJECT_NAME_CHARS:
        raise InvalidValueError(
            f"a project's name is at most {MAX_PROJECT_NAME_CHARS} characters long"
        )
    return name
