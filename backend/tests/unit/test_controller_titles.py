# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

from __future__ import annotations

import pytest

from robinauts.controller.core.titles import ELLIPSIS, MAX_TITLE_CHARS, title_from_text


@pytest.mark.parametrize(
    ("text", "title"),
    [
        ("What is a robinaut?", "What is a robinaut?"),
        ("", ""),
        ("  \n\t\n", ""),
        ("  spaces  around  ", "spaces around"),
        ("\n\nfirst line\nsecond line", "first line"),
        ("hard\x00to\x07show", "hard to show"),
        ("café 中文 \U0001f600", "café 中文 \U0001f600"),
        ("a" * MAX_TITLE_CHARS, "a" * MAX_TITLE_CHARS),
    ],
)
def test_the_title_is_the_first_line_with_anything_on_it(text: str, title: str) -> None:
    assert title_from_text(text) == title


def test_a_long_title_is_cut_at_a_word_boundary() -> None:
    title = title_from_text("word " * 100)
    assert len(title) <= MAX_TITLE_CHARS
    assert title.endswith("word" + ELLIPSIS)


def test_a_long_word_is_cut_at_the_limit() -> None:
    assert title_from_text("a" * 500) == "a" * (MAX_TITLE_CHARS - 1) + ELLIPSIS


def test_a_cut_keeps_accents_with_their_letters() -> None:
    title = title_from_text("é" * MAX_TITLE_CHARS)
    assert title[: -len(ELLIPSIS)].endswith("é")


def test_a_cut_keeps_an_emoji_sequence_whole() -> None:
    family = "\U0001f468‍\U0001f469‍\U0001f466"
    title = title_from_text("x" * (MAX_TITLE_CHARS - 4) + family)
    assert title == "x" * (MAX_TITLE_CHARS - 4) + ELLIPSIS


def test_nothing_left_after_the_cut_is_no_title() -> None:
    assert title_from_text("‍" * (MAX_TITLE_CHARS * 3)) == ""


def test_only_the_beginning_of_a_huge_text_is_read() -> None:
    assert title_from_text("\n" + "the title\n" + "y" * 64_000_000) == "the title"
