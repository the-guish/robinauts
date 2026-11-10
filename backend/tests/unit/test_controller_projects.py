# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

from __future__ import annotations

import pytest

from robinauts.controller.contract.domain import InvalidValueError
from robinauts.controller.core.projects import MAX_PROJECT_NAME_CHARS, project_name


@pytest.mark.parametrize(
    ("text", "name"),
    [
        ("Work", "Work"),
        ("  spaces  around  ", "spaces around"),
        ("two\nlines", "two lines"),
        ("hard\x00to\x07show", "hard to show"),
        ("café 中文", "café 中文"),
        ("a" * MAX_PROJECT_NAME_CHARS, "a" * MAX_PROJECT_NAME_CHARS),
    ],
)
def test_a_name_is_kept_on_one_line_trimmed(text: str, name: str) -> None:
    assert project_name(text) == name


@pytest.mark.parametrize("text", ["", "   ", "\n\t", "\x00", "a" * (MAX_PROJECT_NAME_CHARS + 1)])
def test_a_name_empty_or_too_long_is_refused(text: str) -> None:
    with pytest.raises(InvalidValueError):
        project_name(text)
