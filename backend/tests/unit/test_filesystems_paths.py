# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The one rule that bounds every file operation: what a path inside a folder may say."""

from __future__ import annotations

import pytest

from robinauts.filesystems.contract.domain import (
    MAX_PATH_CHARS,
    MAX_SEGMENT_BYTES,
    PathError,
    joined,
    plain,
    relative_path,
)


@pytest.mark.parametrize(
    ("text", "segments"),
    [
        ("", ()),
        ("a.md", ("a.md",)),
        ("notes/today.md", ("notes", "today.md")),
        ("a/b/c/d", ("a", "b", "c", "d")),
        (".hidden", (".hidden",)),
        ("..dots", ("..dots",)),
        ("a..b", ("a..b",)),
        ("with space/and more.txt", ("with space", "and more.txt")),
        ("ünïcödé/日本語.md", ("ünïcödé", "日本語.md")),
        ("x" * MAX_SEGMENT_BYTES, ("x" * MAX_SEGMENT_BYTES,)),
    ],
)
def test_a_path_inside_the_folder_is_its_segments(text: str, segments: tuple[str, ...]) -> None:
    assert relative_path(text) == segments
    assert joined(segments) == text


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ("/etc/passwd", "starts with '/'"),
        ("/", "starts with '/'"),
        ("notes/", "ends with '/'"),
        ("..", "'..'"),
        ("../x", "'..'"),
        ("a/../b", "'..'"),
        ("a/..", "'..'"),
        (".", "'.'"),
        ("./a", "'.'"),
        ("a/./b", "'.'"),
        ("a//b", "not allowed"),
        ("a\\b", "not allowed"),
        ("a\x00b", "not allowed"),
        ("a\nb", "not allowed"),
        ("a/ /b", "not allowed"),
        ("x" * (MAX_SEGMENT_BYTES + 1), "not allowed"),
        ("é" * 128, "not allowed"),
        ("a/" * (MAX_PATH_CHARS // 2) + "b", f"at most {MAX_PATH_CHARS} characters"),
    ],
)
def test_a_path_that_could_name_anything_else_is_refused(text: str, problem: str) -> None:
    with pytest.raises(PathError, match=problem.replace("'", r"\'").replace(".", r"\.")):
        relative_path(text)


def test_a_path_is_a_string() -> None:
    with pytest.raises(PathError, match="is a string"):
        relative_path(None)  # type: ignore[arg-type]
    with pytest.raises(PathError, match="is a string"):
        relative_path(["a"])  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("name", "is_plain"),
    [
        ("agent-1", True),
        ("3f2a0c7e-8e1b-4b7a-9f44-1f2c3d4e5f60", True),
        ("Notes 2026", True),
        (".hidden", True),
        ("", False),
        (" ", False),
        (".", False),
        ("..", False),
        ("a/b", False),
        ("a\\b", False),
        ("a\tb", False),
        ("x" * (MAX_SEGMENT_BYTES + 1), False),
    ],
)
def test_a_plain_name_is_one_file_or_folder(name: str, is_plain: bool) -> None:
    assert plain(name) is is_plain
