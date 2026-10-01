# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The title: the beginning of the first question, on one line."""

import time
import unicodedata

import pytest

from conversations import answer, conversation, question
from robinauts.legacy.core import (
    ELLIPSIS,
    MAX_TITLE_LINES,
    MAX_TITLE_REACH,
    MAX_TITLE_SCAN,
    derive_title,
    first_question,
    title_from_text,
)
from robinauts.legacy.domain import MAX_TITLE_CHARS, ReasoningPart, TextPart, text_parts


@pytest.mark.parametrize(
    ("text", "title"),
    [
        ("What is a robinaut?", "What is a robinaut?"),
        ("", ""),
        ("   ", ""),
        ("\n\n\t\n", ""),
        ("  spaces  around  ", "spaces around"),
        ("first line\nsecond line", "first line"),
        (
            "\n\n  \nthe first line with anything on it\nand more",
            "the first line with anything on it",
        ),
        ("a\tb\tc", "a b c"),
        ("hard\x00to\x07show", "hard to show"),
        ("café 中文 \U0001f600", "café 中文 \U0001f600"),
        ("a" * MAX_TITLE_CHARS, "a" * MAX_TITLE_CHARS),
    ],
)
def test_a_title_is_made_of_the_first_line_that_has_anything_on_it(text: str, title: str) -> None:
    assert title_from_text(text) == title


def test_a_long_title_is_cut_at_a_word_boundary_within_the_bound() -> None:
    title = title_from_text("word " * 100)
    assert len(title) <= MAX_TITLE_CHARS
    assert title.endswith(ELLIPSIS)
    assert title.startswith("word word")
    assert "  " not in title


def test_a_long_first_word_is_cut_where_the_bound_falls() -> None:
    title = title_from_text("a" * 500)
    assert title == "a" * (MAX_TITLE_CHARS - 1) + ELLIPSIS
    assert len(title) == MAX_TITLE_CHARS


def test_one_character_past_the_bound_is_cut() -> None:
    title = title_from_text("b" * (MAX_TITLE_CHARS + 1))
    assert len(title) == MAX_TITLE_CHARS
    assert title.endswith(ELLIPSIS)


def test_the_title_comes_from_the_conversations_first_question() -> None:
    first = question("the first question", seconds=0)
    said = answer(first, "an answer nobody titles a conversation with", seconds=1)
    second = question("a later question", parent=said, seconds=2)
    assert derive_title([second, said, first]) == "the first question"
    assert first_question([second, said, first]) == first


def test_a_conversation_with_no_question_yet_has_no_title() -> None:
    assert derive_title([]) == ""
    assert first_question([]) is None
    assert first_question([answer(question(), seconds=1)]) is None


def test_a_question_with_no_text_in_it_gives_no_title() -> None:
    assert derive_title([question(parts=(ReasoningPart("thinking"),))]) == ""


def test_the_title_is_the_questions_text_however_it_was_stored() -> None:
    """Text arrives in as many parts as it had to be stored in; where the
    split fell is no business of the title's."""
    message = question(parts=(ReasoningPart("thinking"), TextPart("the ques"), TextPart("tion")))
    assert derive_title([message]) == "the question"


def test_a_cut_never_falls_inside_a_character() -> None:
    accented = "e\u0301" * MAX_TITLE_CHARS
    title = title_from_text(accented)
    assert title.endswith(ELLIPSIS)
    body = title[: -len(ELLIPSIS)]
    # Whole pairs only: the cut fell before a base whose accent would have
    # been left behind, not between the two.
    assert body == "e\u0301" * (len(body) // 2)
    assert unicodedata.combining(body[-1]), "the accent stays with its letter"


def test_nothing_left_to_show_is_no_title_rather_than_an_ellipsis() -> None:
    """A line of nothing but combining marks backs the cut all the way out."""
    assert title_from_text("\u0301" * (MAX_TITLE_CHARS * 3)) == ""
    assert title_from_text("\u200d" * (MAX_TITLE_CHARS * 3)) == ""


def test_a_cut_keeps_an_emoji_sequence_together() -> None:
    family = "\U0001f468\u200d\U0001f469\u200d\U0001f466"
    title = title_from_text(("word " * 30) + family + " and more " * 20)
    assert title.endswith(ELLIPSIS)
    body = title[: -len(ELLIPSIS)]
    assert not body.endswith("\u200d")
    assert family in body or "\U0001f468" not in body


def test_a_joiner_is_not_a_space() -> None:
    """A zero-width joiner holds an emoji sequence together; a title that
    replaced it with a space would show the members instead of the family."""
    family = "\U0001f468\u200d\U0001f469\u200d\U0001f466"
    assert title_from_text(f"the {family} emoji") == f"the {family} emoji"
    assert conversation(title=title_from_text(family)).title == family


def test_a_derived_title_is_one_a_conversation_will_accept() -> None:
    for text in ("a" * 500, "first\nsecond", "  padded  ", "hard\x00to\x07show"):
        assert conversation(title=title_from_text(text)).title == title_from_text(text)


def test_a_title_costs_a_bounded_amount_of_work() -> None:
    """A first message can be a pasted file, and the title of one is a
    hundred and twenty characters: reading all of it on the event loop is
    what this guards against."""
    pasted = "x" * 64_000_000
    started = time.monotonic()
    assert title_from_text(pasted) == "x" * (MAX_TITLE_CHARS - 1) + ELLIPSIS
    assert title_from_text("\n\n\n" + "the title\n" + "y" * 64_000_000) == "the title"
    # And through a message, whose parts are each bounded, so a pasted file
    # arrives as a great many of them (``domain.text_parts``).
    assert derive_title([question(parts=text_parts("x" * 2_000_000))]).endswith(ELLIPSIS)
    spent = time.monotonic() - started
    assert spent < 1.0, f"three titles of sixty-four million characters took {spent:.2f}s"


def test_a_message_that_begins_with_nothing_for_pages_has_no_title() -> None:
    """The right answer for one nobody could read the beginning of either."""
    assert title_from_text("\n" * (MAX_TITLE_LINES + 1) + "too far down") == ""
    assert title_from_text(" " * (MAX_TITLE_REACH + 10) + "\ntoo far in") == ""
    assert title_from_text(" " * (MAX_TITLE_SCAN * 2) + "past what a line shows") == ""
