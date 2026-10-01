# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""A conversation's title, derived from its first question.

Generated titles are planned and out of this version
(``docs/working-notes/poc-scope.md``): the title is the beginning of the first
message, which is also what the spec falls back to when asking the model for
one fails. Deriving it is pure, so the same conversation is always titled the
same way, whoever asks.

What it is, exactly: the first line with anything on it of the first question's
text, its spaces collapsed, cut at a word boundary to ``MAX_TITLE_CHARS`` with
an ellipsis if it is longer. A message with no text in it -- an image, a file,
when those arrive -- gives no title, and neither does one that is only
whitespace.

**Bounded work.** A first message can be a pasted file: sixty megabytes of it,
on the event loop, for a hundred and twenty characters of title. So only the
beginning is ever looked at -- ``MAX_TITLE_SCAN`` characters of a line, at
most ``MAX_TITLE_LINES`` blank lines skipped, and never further than
``MAX_TITLE_REACH`` into the text -- and the lines are found by searching for
the newline rather than by cutting the text into all of them. A message that
begins with more blank than that has no title, which is the right answer for
one nobody could read the beginning of either.

**A cut never falls inside a character.** Combining marks stay with what they
mark, and the joiners of an emoji sequence stay with the sequence: a cut that
left a base without its accent would change the letter, and one that left half
a family would show two people and a stranded joiner. This is not full
grapheme clustering -- the standard library has no such thing -- it is the two
cases that turn up in the text people write.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable

from robinauts.legacy.domain import JOINERS, MAX_TITLE_CHARS, Message, Role

ELLIPSIS = "…"
"""What a cut title ends in, so that a truncation reads as one."""

MAX_TITLE_SCAN = 512
"""How much of a line a title can be made of. Four times the bound itself."""

MAX_TITLE_LINES = 64
"""How many lines with nothing on them a title is looked for past."""

MAX_TITLE_REACH = 64 * 1024
"""How far into a message a title is looked for at all."""


def title_from_text(text: str) -> str:
    """A title made of ``text``; the empty string if there is nothing to show.

    Also what a rename goes through, so that a title typed by hand and one
    derived from a message are the same kind of thing.
    """
    start = 0
    for _ in range(MAX_TITLE_LINES):
        if start >= len(text) or start > MAX_TITLE_REACH:
            break
        # Searching for the newline rather than splitting: a line ends at one,
        # and cutting the whole text into lines would read all of it.
        ends = text.find("\n", start, start + MAX_TITLE_SCAN + 1)
        looked_at = text[start:ends] if ends != -1 else text[start : start + MAX_TITLE_SCAN]
        cleaned = _shown(looked_at)
        if cleaned:
            return _cut(cleaned)
        if ends == -1:
            # A line longer than we look at, with nothing on the part we did.
            ends = text.find("\n", start + MAX_TITLE_SCAN)
            if ends == -1:
                break
        start = ends + 1
    return ""


def _shown(line: str) -> str:
    """One line, as a title may carry it: printable, spaces collapsed.

    A control character becomes a space rather than being dropped: a title is
    drawn in a list, a tab and a heading, none of which survive one, and
    dropping a tab would run two words together. The joiners are kept: they
    are inside a word, not between two.
    """
    printable = "".join(
        character if character.isprintable() or character in JOINERS else " " for character in line
    )
    return " ".join(printable.split())


def _cut(title: str) -> str:
    """``title`` within the bound, at a word boundary where there is one.

    Nothing left to show is no title: a line of nothing but combining marks
    backs the cut all the way out, and a bare ellipsis says less than an
    empty title does.
    """
    if len(title) <= MAX_TITLE_CHARS:
        return title
    kept = title[: MAX_TITLE_CHARS - 1]
    head, space, _ = kept.rpartition(" ")
    # A word boundary only if it leaves most of the title: a first "word" of
    # a hundred characters would otherwise be cut down to nothing.
    if space and len(head) >= MAX_TITLE_CHARS // 2:
        kept = head
    shown = _whole(title, len(kept)).rstrip()
    return shown + ELLIPSIS if shown else ""


def _whole(title: str, end: int) -> str:
    """``title`` up to ``end``, moved back out of any character it fell inside."""
    while end > 0 and _continues(title, end):
        end -= 1
    while end > 0 and title[end - 1] in JOINERS:
        end -= 1
    return title[:end]


def _continues(title: str, end: int) -> bool:
    """Whether what follows ``end`` belongs to the character before it."""
    if end >= len(title):
        return False
    following = title[end]
    if unicodedata.combining(following) or following in JOINERS:
        return True
    return title[end - 1] in JOINERS


def first_question(messages: Iterable[Message]) -> Message | None:
    """The conversation's first question: the oldest message of a person."""
    questions = [message for message in messages if message.role is Role.USER]
    if not questions:
        return None
    return min(questions, key=lambda message: (message.created_at, message.id.bytes))


def derive_title(messages: Iterable[Message]) -> str:
    """The title of a conversation holding ``messages``; empty if it has none.

    From the message's text as a whole, which is what the person wrote: text
    arrives in as many parts as it had to be stored in
    (``robinauts.legacy.domain.text_parts``), and where the split fell is no business
    of the title's.
    """
    question = first_question(messages)
    if question is None:
        return ""
    return title_from_text(question.text)
