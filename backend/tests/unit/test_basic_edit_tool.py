# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The basic edit tools: what each does to the folder it is given, what it says back, and what
it refuses, in words the model can act on. Over a folder in memory, which keeps the folder's
contract; the folders themselves are tested on their own."""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from typing import Any

import pytest

from robinauts.agent_engines.contract.domain import FunctionTool, FunctionToolError
from robinauts.tools import basic_edit
from robinauts.tools.basic_edit import arguments, read
from robinauts.tools.contract.ports import TurnScope
from util.aio import asyncio_test
from util.memory_folder import MemoryFolder

NAMES = [
    "read_file",
    "list_folder",
    "write_file",
    "replace_in_file",
    "replace_lines",
    "move_file",
    "copy_file",
    "delete_file",
]


def tools(folder: MemoryFolder) -> dict[str, FunctionTool]:
    scope = TurnScope(uuid.uuid4(), "keeper", None, folder)  # type: ignore[arg-type]
    return {tool.name: tool for tool in basic_edit.load().tools_for(scope)}


async def call(folder: MemoryFolder, name: str, arguments: Mapping[str, Any]) -> Any:
    return json.loads(await tools(folder)[name].call(arguments))


async def refused(
    folder: MemoryFolder, name: str, arguments: Mapping[str, Any], match: str
) -> None:
    with pytest.raises(FunctionToolError, match=match):
        await tools(folder)[name].call(arguments)


def with_files(**files: str) -> MemoryFolder:
    folder = MemoryFolder()
    for path, content in files.items():
        segments = tuple(path.split("/"))
        folder.files[segments] = content.encode()
        for depth in range(len(segments)):
            folder.folders.add(segments[:depth])
    return folder


def test_load_gives_the_tool_its_id_and_eight_functions() -> None:
    provider = basic_edit.load()
    assert provider.id == "basic_edit"
    found = tools(MemoryFolder())
    assert list(found) == NAMES
    assert found["read_file"].input_schema["required"] == ["path"]
    assert found["replace_lines"].input_schema["required"] == [
        "path",
        "from_line",
        "to_line",
        "content",
    ]
    assert all(not f.input_schema["additionalProperties"] for f in found.values())


def test_a_turn_with_no_folder_cannot_have_the_tool() -> None:
    with pytest.raises(RuntimeError, match="no folder"):
        basic_edit.load().tools_for(TurnScope(uuid.uuid4(), "keeper", None))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("name", "given", "problem"),
    [
        ("read_file", {}, "Missing argument"),
        ("read_file", {"path": "a.md", "extra": 1}, "Unknown argument"),
        ("read_file", {"path": 7}, "`path` must be a string"),
        ("read_file", {"path": ""}, "`path` must name a file"),
        ("read_file", {"path": "/etc/passwd"}, "starts with '/'"),
        ("read_file", {"path": "../a.md"}, "'..'"),
        ("read_file", {"path": "a.md", "from_line": 0}, "`from_line` must be at least 1"),
        ("read_file", {"path": "a.md", "from_line": "1"}, "`from_line` must be a whole number"),
        ("read_file", {"path": "a.md", "from_line": True}, "`from_line` must be a whole number"),
        ("read_file", {"path": "a.md", "from_line": 3, "to_line": 2}, "not be before"),
        ("read_file", {"path": "a.md", "tail": 2, "to_line": 2}, "`tail` cannot be given"),
        ("list_folder", {"path": "a/"}, "ends with '/'"),
        ("list_folder", {"depth": 0}, "from 1 to 5"),
        ("list_folder", {"depth": 6}, "from 1 to 5"),
        ("write_file", {"path": "a.md"}, "Missing argument"),
        ("write_file", {"path": "a.md", "content": 1}, "`content` must be a string"),
        ("replace_in_file", {"path": "a.md", "old": "", "new": "x"}, "`old` must not be empty"),
        ("replace_lines", {"path": "a.md", "from_line": 1, "to_line": 1}, "Missing argument"),
        (
            "replace_lines",
            {"path": "a.md", "from_line": 1, "to_line": -1, "content": ""},
            "`to_line`",
        ),
        ("move_file", {"path": "a.md"}, "Missing argument"),
        ("move_file", {"path": "a.md", "to": ""}, "`to` must name a file"),
        ("copy_file", {"path": "a.md", "to": "b\\c.md"}, "`to`: .* has a name that is not allowed"),
        ("delete_file", {"path": "."}, "'.'"),
    ],
)
@asyncio_test
async def test_wrong_arguments_are_refused_before_the_folder_is_touched(
    name: str, given: Mapping[str, Any], problem: str
) -> None:
    folder = with_files(**{"a.md": "one\n"})
    await refused(folder, name, given, problem.replace("(", r"\(").replace(")", r"\)"))
    assert folder.files == {("a.md",): b"one\n"}


# --- read_file ---------------------------------------------------------------------


@asyncio_test
async def test_read_file_gives_the_whole_file_numbered() -> None:
    folder = with_files(**{"notes/a.md": "one\ntwo\nthree\n"})
    assert await call(folder, "read_file", {"path": "notes/a.md"}) == {
        "path": "notes/a.md",
        "total_lines": 3,
        "from_line": 1,
        "to_line": 3,
        "truncated": False,
        "content": "one\ntwo\nthree\n",
    }


@asyncio_test
async def test_read_file_gives_a_range_the_head_or_the_tail() -> None:
    folder = with_files(**{"a.md": "1\n2\n3\n4\n5"})
    ranged = await call(folder, "read_file", {"path": "a.md", "from_line": 2, "to_line": 3})
    assert (ranged["from_line"], ranged["to_line"], ranged["content"]) == (2, 3, "2\n3\n")
    head = await call(folder, "read_file", {"path": "a.md", "to_line": 2})
    assert (head["from_line"], head["to_line"], head["content"]) == (1, 2, "1\n2\n")
    past = await call(folder, "read_file", {"path": "a.md", "from_line": 4, "to_line": 99})
    assert (past["to_line"], past["content"], past["truncated"]) == (5, "4\n5", False)
    tail = await call(folder, "read_file", {"path": "a.md", "tail": 2})
    assert (tail["from_line"], tail["to_line"], tail["content"]) == (4, 5, "4\n5")
    whole_tail = await call(folder, "read_file", {"path": "a.md", "tail": 99})
    assert (whole_tail["from_line"], whole_tail["to_line"]) == (1, 5)


@asyncio_test
async def test_read_file_of_an_empty_file_and_past_its_end() -> None:
    folder = with_files(**{"empty.md": "", "a.md": "one\n"})
    assert await call(folder, "read_file", {"path": "empty.md"}) == {
        "path": "empty.md",
        "total_lines": 0,
        "from_line": 0,
        "to_line": 0,
        "truncated": False,
        "content": "",
    }
    await refused(folder, "read_file", {"path": "a.md", "from_line": 2}, "has 1 lines")


@asyncio_test
async def test_a_long_read_is_cut_and_says_where_to_go_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(read, "MAX_LINES_PER_READ", 2)
    folder = with_files(**{"a.md": "1\n2\n3\n4\n"})
    first = await call(folder, "read_file", {"path": "a.md"})
    assert (first["to_line"], first["truncated"], first["next_line"]) == (2, True, 3)
    assert first["content"] == "1\n2\n"
    rest = await call(folder, "read_file", {"path": "a.md", "from_line": first["next_line"]})
    assert (rest["to_line"], rest["truncated"], rest["content"]) == (4, False, "3\n4\n")
    monkeypatch.setattr(read, "MAX_LINES_PER_READ", 1000)
    monkeypatch.setattr(read, "MAX_CHARS_PER_READ", 5)
    by_chars = await call(folder, "read_file", {"path": "a.md"})
    assert (by_chars["to_line"], by_chars["truncated"]) == (2, True)
    # One line is always answered, however long.
    folder = with_files(**{"long.md": "x" * 50 + "\nshort\n"})
    one = await call(folder, "read_file", {"path": "long.md"})
    assert (one["to_line"], one["truncated"], one["content"]) == (1, True, "x" * 50 + "\n")


@asyncio_test
async def test_read_file_refuses_what_is_not_a_text_file() -> None:
    folder = with_files(**{"a/b.md": "x"})
    folder.files[("bin",)] = b"\xff\xfe\x00"
    await refused(folder, "read_file", {"path": "nope.md"}, "Nothing at 'nope.md'")
    await refused(folder, "read_file", {"path": "a"}, "'a' is a folder")
    await refused(folder, "read_file", {"path": "bin"}, "not a UTF-8 text file")


# --- list_folder -------------------------------------------------------------------


@asyncio_test
async def test_list_folder_lists_one_level_or_deeper_each_folder_right_after_it() -> None:
    folder = with_files(**{"b.md": "bb", "a/one.md": "1", "a/sub/deep.md": "deep", "a/two.md": "2"})
    top = await call(folder, "list_folder", {})
    assert (top["path"], top["depth"], top["truncated"]) == ("", 1, False)
    assert [(e["path"], e["kind"]) for e in top["entries"]] == [("a", "folder"), ("b.md", "file")]
    assert top["entries"][1]["size"] == 2
    assert "size" not in top["entries"][0]
    assert all("modified_at" in e for e in top["entries"])
    deep = await call(folder, "list_folder", {"path": "a", "depth": 2})
    assert [e["path"] for e in deep["entries"]] == [
        "a/one.md",
        "a/sub",
        "a/sub/deep.md",
        "a/two.md",
    ]
    assert await call(folder, "list_folder", {"path": "a/sub", "depth": 5}) == {
        "path": "a/sub",
        "depth": 5,
        "entries": [{"path": "a/sub/deep.md", "kind": "file", "size": 4} | _modified(deep)],
        "truncated": False,
    }
    assert (await call(MemoryFolder(), "list_folder", {}))["entries"] == []


def _modified(listing: dict[str, Any]) -> dict[str, Any]:
    return {"modified_at": listing["entries"][0]["modified_at"]}


@asyncio_test
async def test_list_folder_is_cut_at_max_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(read, "MAX_ENTRIES", 3)
    folder = with_files(**{f"{n}.md": "x" for n in range(5)})
    listed = await call(folder, "list_folder", {})
    assert [e["path"] for e in listed["entries"]] == ["0.md", "1.md", "2.md"]
    assert listed["truncated"] is True


@asyncio_test
async def test_list_folder_refuses_a_file_and_a_folder_that_is_not_there() -> None:
    folder = with_files(**{"a.md": "x"})
    await refused(folder, "list_folder", {"path": "a.md"}, "'a.md' is a file, not a folder")
    await refused(folder, "list_folder", {"path": "nope"}, "Nothing at 'nope'")


# --- write_file, replace_in_file, replace_lines -------------------------------------


@asyncio_test
async def test_write_file_creates_or_replaces_whole() -> None:
    folder = MemoryFolder()
    assert await call(folder, "write_file", {"path": "n/a.md", "content": "héllo\n"}) == {
        "path": "n/a.md",
        "bytes": 7,
        "created": True,
    }
    assert await call(folder, "write_file", {"path": "n/a.md", "content": "x"}) == {
        "path": "n/a.md",
        "bytes": 1,
        "created": False,
    }
    assert folder.files == {("n", "a.md"): b"x"}


@asyncio_test
async def test_a_file_past_the_size_is_refused_read_and_written(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(arguments, "MAX_FILE_BYTES", 10)
    folder = with_files(**{"big.md": "x" * 11, "a.md": "12345"})
    await refused(folder, "write_file", {"path": "a.md", "content": "x" * 11}, "longer than 10")
    await refused(folder, "read_file", {"path": "big.md"}, "at most 10 bytes")
    await refused(
        folder, "replace_in_file", {"path": "a.md", "old": "1", "new": "x" * 7}, "11 bytes"
    )
    await refused(
        folder,
        "replace_lines",
        {"path": "a.md", "from_line": 2, "to_line": 1, "content": "x" * 6},
        "12 bytes",
    )
    assert folder.files[("a.md",)] == b"12345"


@asyncio_test
async def test_a_replacement_that_would_outgrow_the_file_is_refused_before_it_is_built() -> None:
    # Built first, the result would be 10 GB: the refusal has to come from the counts alone.
    folder = with_files(**{"a.md": "a" * 100_000})
    given = {"path": "a.md", "old": "a", "new": "b" * 100_000}
    await refused(folder, "replace_in_file", given, "would make 'a.md' 10000000000 bytes")
    assert folder.files[("a.md",)] == b"a" * 100_000
    # Counted in bytes of UTF-8, not in characters: three bytes each, three times.
    folder = with_files(**{"b.md": "x" * 1_048_570 + "aaa"})
    given = {"path": "b.md", "old": "a", "new": "éé"}
    await refused(folder, "replace_in_file", given, "would make 'b.md' 1048582 bytes")
    given["new"] = "é"
    assert await call(folder, "replace_in_file", given) == {"path": "b.md", "replacements": 3}
    assert len(folder.files[("b.md",)]) == 1_048_576


@asyncio_test
async def test_replace_in_file_replaces_the_text_every_time_it_occurs() -> None:
    folder = with_files(**{"a.md": "the cat and the dog\nthe end\n"})
    assert await call(folder, "replace_in_file", {"path": "a.md", "old": "the ", "new": "a "}) == {
        "path": "a.md",
        "replacements": 3,
    }
    assert folder.files[("a.md",)] == b"a cat and a dog\na end\n"
    assert await call(folder, "replace_in_file", {"path": "a.md", "old": "a end\n", "new": ""}) == {
        "path": "a.md",
        "replacements": 1,
    }
    assert folder.files[("a.md",)] == b"a cat and a dog\n"
    await refused(folder, "replace_in_file", {"path": "a.md", "old": "The", "new": "x"}, "nowhere")
    await refused(folder, "replace_in_file", {"path": "a.md", "old": "c.t", "new": "x"}, "nowhere")
    assert folder.files[("a.md",)] == b"a cat and a dog\n"


@pytest.mark.parametrize(
    ("before", "from_line", "to_line", "content", "after", "removed", "added"),
    [
        ("1\n2\n3\n", 2, 2, "TWO\n", "1\nTWO\n3\n", 1, 1),
        ("1\n2\n3\n", 2, 2, "a\nb\nc", "1\na\nb\nc\n3\n", 1, 3),
        ("1\n2\n3\n", 1, 3, "only", "only\n", 3, 1),
        ("1\n2\n3\n", 2, 3, "", "1\n", 2, 0),
        ("1\n2\n3\n", 2, 1, "between\n", "1\nbetween\n2\n3\n", 0, 1),
        ("1\n2\n3\n", 4, 3, "4\n", "1\n2\n3\n4\n", 0, 1),
        ("1\n2\n3", 3, 3, "three", "1\n2\nthree", 1, 1),
        ("1\n2\n3", 4, 3, "4", "1\n2\n3\n4", 0, 1),
        ("", 1, 0, "first\nsecond\n", "first\nsecond\n", 0, 2),
        ("1\n", 1, 1, "", "", 1, 0),
    ],
)
@asyncio_test
async def test_replace_lines_replaces_inserts_appends_and_deletes(
    before: str, from_line: int, to_line: int, content: str, after: str, removed: int, added: int
) -> None:
    folder = with_files(**{"a.md": before})
    given = {"path": "a.md", "from_line": from_line, "to_line": to_line, "content": content}
    assert await call(folder, "replace_lines", given) == {
        "path": "a.md",
        "from_line": from_line,
        "to_line": to_line,
        "lines_removed": removed,
        "lines_added": added,
        "total_lines": after.count("\n") + (0 if after.endswith("\n") or not after else 1),
    }
    assert folder.files[("a.md",)] == after.encode()


@pytest.mark.parametrize(
    ("from_line", "to_line", "problem"),
    [
        (5, 5, "`from_line` can be at most 4"),
        (3, 1, "`to_line` must be at least `from_line` - 1"),
        (2, 4, "`to_line` can be at most 3"),
    ],
)
@asyncio_test
async def test_replace_lines_refuses_numbers_the_file_does_not_have(
    from_line: int, to_line: int, problem: str
) -> None:
    folder = with_files(**{"a.md": "1\n2\n3\n"})
    given = {"path": "a.md", "from_line": from_line, "to_line": to_line, "content": "x"}
    await refused(folder, "replace_lines", given, problem.replace("`", "`"))
    assert folder.files[("a.md",)] == b"1\n2\n3\n"


# --- move_file, copy_file, delete_file ------------------------------------------------


@asyncio_test
async def test_move_copy_and_delete_take_one_file_and_say_what_they_did() -> None:
    folder = with_files(**{"a.md": "a", "b.md": "b"})
    assert await call(folder, "move_file", {"path": "a.md", "to": "x/c.md"}) == {
        "path": "a.md",
        "to": "x/c.md",
    }
    assert await call(folder, "copy_file", {"path": "b.md", "to": "x/d.md"}) == {
        "path": "b.md",
        "to": "x/d.md",
    }
    assert folder.files == {("b.md",): b"b", ("x", "c.md"): b"a", ("x", "d.md"): b"b"}
    assert await call(folder, "delete_file", {"path": "x/c.md"}) == {
        "path": "x/c.md",
        "deleted": "file",
    }
    await refused(folder, "delete_file", {"path": "x"}, "not empty")
    await call(folder, "delete_file", {"path": "x/d.md"})
    assert await call(folder, "delete_file", {"path": "x"}) == {"path": "x", "deleted": "folder"}
    assert folder.files == {("b.md",): b"b"}


@asyncio_test
async def test_the_folders_refusals_reach_the_model_in_its_words() -> None:
    folder = with_files(**{"a.md": "a", "x/b.md": "b"})
    await refused(folder, "move_file", {"path": "a.md", "to": "x/b.md"}, "already a file")
    await refused(folder, "move_file", {"path": "x", "to": "y"}, "'x' is a folder")
    await refused(folder, "copy_file", {"path": "nope.md", "to": "y.md"}, "Nothing at 'nope.md'")
    await refused(folder, "copy_file", {"path": "a.md", "to": "a.md/under.md"}, "is a file")
    await refused(folder, "delete_file", {"path": "nope.md"}, "Nothing at 'nope.md'")
    await refused(folder, "write_file", {"path": "x", "content": ""}, "'x' is a folder")
    assert folder.files == {("a.md",): b"a", ("x", "b.md"): b"b"}
