# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Text files as lines: what ``read_file`` numbers and ``replace_lines`` edits.

A line ends at ``\\n`` and nowhere else, so that the numbers these tools say are the ones a
person's editor says. A file is UTF-8; one that is not is refused rather than read wrong.
"""

from __future__ import annotations

from robinauts.agent_engines.contract.domain import FunctionToolError

NEWLINE = "\n"


def decode(data: bytes, path: str) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise FunctionToolError(
            f"{path!r} is not a UTF-8 text file; these tools read and edit text files alone."
        ) from error


def split(text: str) -> tuple[list[str], bool]:
    """The lines of ``text`` without their newlines, and whether its last line ends with one.
    An empty text has no lines; ``"a\\n"`` and ``"a"`` are one line each."""
    if text == "":
        return [], True
    lines = text.split(NEWLINE)
    if lines[-1] == "":
        return lines[:-1], True
    return lines, False


def join(lines: list[str], ends_with_newline: bool) -> str:
    """The inverse of ``split``."""
    if not lines:
        return ""
    return NEWLINE.join(lines) + (NEWLINE if ends_with_newline else "")
