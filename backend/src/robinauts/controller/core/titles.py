# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

from __future__ import annotations

import unicodedata

MAX_TITLE_CHARS = 120
ELLIPSIS = "…"

# Only the beginning of a message is read: the first one may be a pasted file.
MAX_SCAN_CHARS = 64 * 1024

# Zero-width joiner and the variation selectors: not printable for Python, but they
# hold an emoji sequence together.
JOINERS = frozenset({"‍", "︎", "️"})


def title_from_text(text: str) -> str:
    for line in text[:MAX_SCAN_CHARS].splitlines():
        shown = " ".join(_printable(line).split())
        if shown:
            return _cut(shown)
    return ""


def _printable(line: str) -> str:
    return "".join(c if c.isprintable() or c in JOINERS else " " for c in line)


def _cut(title: str) -> str:
    if len(title) <= MAX_TITLE_CHARS:
        return title
    end = MAX_TITLE_CHARS - len(ELLIPSIS)
    word_end = title.rfind(" ", 0, end + 1)
    if word_end >= MAX_TITLE_CHARS // 2:
        end = word_end
    # Never split a letter from its accent, nor an emoji sequence.
    while end > 0 and _inside_a_character(title, end):
        end -= 1
    kept = title[:end].rstrip()
    return kept + ELLIPSIS if kept else ""


def _inside_a_character(title: str, end: int) -> bool:
    following = title[end]
    return unicodedata.combining(following) > 0 or following in JOINERS or title[end - 1] in JOINERS
